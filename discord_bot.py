"""Discord and Railway control plane for GBP-budgeted Kraken GBP-market DCA.

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
from google.genai import types
import requests

from dca_config import (
    ALLOWED_TARGETS,
    ConfigError,
    DAILY_ANALYSIS_EXPECTED_BY,
    amount_for_tier_gbp,
    awaiting_daily_analysis_refresh,
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
START_DAY_ANALYSIS_EXPECTED_BY = DAILY_ANALYSIS_EXPECTED_BY

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
# Disable intent invalidates overlapping reviews, including reads still in flight.
_enable_review_revisions: dict[str, int] = {target: 0 for target in ALLOWED_TARGETS}

# symbol -> absolute decision and execution-state metadata.
_dca_schedule: dict[str, dict[str, Any]] = {}
_pending_recovery_symbols: set[str] = set()
_pending_gist_delivery_symbols: set[str] = set()
_awaiting_start_day_symbols: set[str] = set()
_awaiting_daily_analysis = False
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
    channel_id = str(getattr(getattr(message, "channel", None), "id", ""))
    return bool(
        allowed and author_id and author_id in allowed
        and CHANNEL_ID and channel_id == str(CHANNEL_ID)
        and not getattr(getattr(message, "author", None), "bot", False)
    )


def _safe_text(value: Any) -> str:
    """Escape external display text without silently dropping warning content."""

    text = str(value)
    for name in ("GH_PAT", "DISCORD_BOT_TOKEN", "GEMINI_API_KEY", TOKEN_ENV):
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    text = " ".join(text.split())
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def _message_parts(content: str, *, limit: int = 1_900) -> list[str]:
    """Keep complete lines where possible; split oversized lines losslessly.

    Leave space below Discord's 2,000-character ceiling. Each section/card is
    sent separately, so a verbose override cannot hide any later asset.
    """

    if limit < 2:
        raise ValueError("Discord message limit must permit a complete character")
    def units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2
    parts: list[str] = []
    pending = ""
    for line in content.splitlines(keepends=True):
        while units(line) > limit:
            if pending:
                parts.append(pending)
                pending = ""
            split_at = limit
            while units(line[:split_at]) > limit:
                split_at -= 1
            parts.append(line[:split_at])
            line = line[split_at:]
        if units(pending) + units(line) > limit:
            parts.append(pending)
            pending = ""
        pending += line
    if pending:
        parts.append(pending)
    return parts


async def _reply_sections(
    message: discord.Message, sections: list[str], *, color: int = 0x5865F2
) -> None:
    for section in sections:
        for part in _message_parts(section):
            # Native cards wrap naturally in both desktop and mobile Discord.
            # One bounded description per message stays below both embed and
            # aggregate limits, including unusually long recovery warnings.
            await message.reply(
                embed=discord.Embed(description=part, color=color),
                allowed_mentions=discord.AllowedMentions.none(),
            )


def _workflow_link(workflow_file: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_REPO):
        return "Open GitHub Actions to follow the workflow."
    return f"[Follow workflow](https://github.com/{GITHUB_REPO}/actions/workflows/{workflow_file})"


def _unconfirmed_dispatch(workflow_file: str) -> str:
    return (
        "Dispatch not confirmed; GitHub may already have accepted it. "
        "Check the workflow and `show status` before retrying.\n"
        + _workflow_link(workflow_file)
    )


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
    "ethereum": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "dogecoin": "DOGE",
}


def _normalise_usd_key(value: str) -> str:
    raw = value.strip()
    lowered = raw.lower()
    raw = _FULL_NAMES.get(lowered, raw).upper().replace("/", "_")
    aliases = {
        "BTC": "BTC_GBP", "BTC_GBP": "BTC_GBP",
        "ETH": "ETH_GBP", "ETH_GBP": "ETH_GBP",
        "SOL": "SOL_GBP", "SOL_GBP": "SOL_GBP",
        "DOGE": "DOGE_GBP", "DOGE_GBP": "DOGE_GBP",
    }
    key = aliases.get(raw)
    if key not in ALLOWED_TARGETS:
        raise ValueError("Supported assets are BTC/GBP, ETH/GBP, SOL/GBP, DOGE/GBP")
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
    try:
        value = response.json().get("value")
    except (ValueError, AttributeError):
        _log(f"ERROR repository variable read failed for {name}: invalid JSON response")
        return None
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


AI_MODEL_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
GEMINI_TIMEOUT_SECONDS = 15
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
        "🐙 Hi! I’m your Kraken DCA guide. Ask me how the bot works, why a pair "
        "is paused, or whether analysis and scheduling are healthy."
    ),
    "dca": (
        "🪙 DCA invests a configured amount on a repeating schedule. This bot "
        "uses deterministic Kraken candle analysis to choose the daily time and "
        "budget tier; DCA reduces timing concentration but cannot remove crypto risk."
    ),
    "regimes": (
        "🧭 UPTREND requires the latest 3 consecutive completed daily Kraken "
        "closes above each candle’s own SMA150. The first break returns SIDEWAYS; "
        "DOWNTREND requires 3 closes below their SMA150 values plus a bearish "
        "latest EMA20/EMA50, and DOWNTREND keeps the configured higher purchase "
        "tier. Weekly EMA and SMA150 slope remain informational. An emergency "
        "per-target override remains visibly labelled until the 3-close rule confirms."
    ),
    "timing": (
        "⏰ Daily analysis compares 3/5/7/14/30/45/60-day windows in 15-minute "
        "Bangkok slots. Type `show status` for today’s selected time, effective "
        "execution time, decision age, and any reason a pair is blocked."
    ),
    "risk": (
        "🛡️ Crypto prices can move sharply and losses are possible. Natural-language "
        "chat is read-only, every trading change requires an exact allowlisted "
        "command, and invalid or stale state fails closed."
    ),
    "markets": (
        "📈 The configured markets are BTC/GBP, ETH/GBP, SOL/GBP, and DOGE/GBP. All four "
        "spend GBP directly on Kraken. "
        "Type `show status` to see which pairs are currently enabled."
    ),
    "controls": (
        "🔐 I can explain a change in normal language, but chat cannot apply it. "
        "Type `help` and use the exact allowlisted `!dca` command shown there; "
        "I will never claim a natural-language request changed trading."
    ),
    "capabilities": (
        "💬 Ask me about DCA, regimes, timing, risk, markets, pair status, or bot "
        "health. I can route read-only requests and explain the system; type `help` "
        "for the exact controls."
    ),
}
CLASSIFY_PROMPT = """You are the read-only intent and topic classifier for a
Kraken DCA Discord bot. Understand ordinary conversational language, but do not
write a reply or provide financial advice.

Choose exactly one action:
- portfolio: explicitly request a read-only Kraken holdings/balance report.
- status: ask about pair enablement, current regimes, budgets, decisions, or times.
- health: ask whether the bot, scheduler, workflows, or configuration are healthy.
- help: ask for commands or instructions.
- chat: any other greeting, explanation, educational question, or request to
  change settings, run analysis, or buy. For chat choose exactly one topic from
  greeting, dca, regimes, timing, risk, markets, controls, or capabilities.
- unknown: only an empty or impossible-to-understand message.

Natural language is strictly read-only. Never return an action that changes a
budget, runs analysis, enables or disables a pair, confirms a change, or places
an order. Requests for those actions must be chat/controls and must never be
described as completed. Do not claim access to live prices or news. The exact
markets are BTC/GBP, ETH/GBP, SOL/GBP, and DOGE/GBP. Deterministic Python—not Gemini—owns
regimes, amounts, timing, analysis, and execution.

Respond as JSON only with exactly `action` and `topic`. For non-chat actions use
topic `capabilities`. Do not return prose, parameters, commands, prices, amounts,
assets, or any additional fields."""

INTENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": sorted(VALID_ACTIONS)},
        "topic": {"type": "string", "enum": sorted(VALID_CHAT_TOPICS)},
    },
    "required": ["action", "topic"],
    "additionalProperties": False,
}


def _rule_based_read_only_intent(text: str) -> dict[str, Any]:
    """Keep safe, common requests useful when Gemini is unavailable."""

    lowered = text.casefold().strip()
    if re.search(
        r"\b(enable|disable|change|set|increase|decrease|buy|purchase|order|"
        r"analyse|analyze)\b",
        lowered,
    ):
        action, topic = "chat", "controls"
    elif any(word in lowered for word in ("help", "command", "how do i")):
        action, topic = "help", "capabilities"
    elif any(word in lowered for word in ("portfolio", "balance", "holding")):
        action, topic = "portfolio", "capabilities"
    elif any(word in lowered for word in ("health", "healthy", "online", "scheduler")):
        action, topic = "health", "capabilities"
    elif any(
        word in lowered
        for word in (
            "status",
            "enabled",
            "disabled",
            "regime",
            "trend",
            "amount",
            "execution time",
            "next buy",
        )
    ):
        action, topic = "status", "capabilities"
    elif any(word in lowered for word in ("hello", "hi ", "hey", "good morning")):
        action, topic = "chat", "greeting"
    elif "risk" in lowered or "safe" in lowered:
        action, topic = "chat", "risk"
    elif "time" in lowered or "schedule" in lowered:
        action, topic = "chat", "timing"
    elif "market" in lowered or "pair" in lowered:
        action, topic = "chat", "markets"
    elif "dca" in lowered:
        action, topic = "chat", "dca"
    elif lowered:
        action, topic = "chat", "capabilities"
    else:
        action, topic = "unknown", "capabilities"
    return _validate_intent({"action": action, "topic": topic})


def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intent, dict) or intent.get("action") not in VALID_ACTIONS:
        return {
            "action": "unknown",
            "params": {},
            "reply": "",
            "topic": "capabilities",
        }
    action = intent["action"]
    topic = str(intent.get("topic") or "capabilities")
    if topic not in VALID_CHAT_TOPICS:
        topic = "capabilities"
    return {
        "action": action,
        # Model-supplied parameters never influence a workflow dispatch.
        "params": {},
        # Model prose is never posted. Gemini selects a reviewed explanation.
        "reply": CHAT_TOPIC_REPLIES[topic] if action == "chat" else "",
        "topic": topic,
    }


async def classify_intent(text: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        return _rule_based_read_only_intent(text)
    prompt = f"{CLASSIFY_PROMPT}\n\nUser message: {text[:1_500]}"
    last_error: Exception | None = None
    for model in AI_MODEL_CANDIDATES:
        try:
            def generate():
                with genai.Client(
                    api_key=GEMINI_API_KEY,
                    http_options=types.HttpOptions(
                        timeout=GEMINI_TIMEOUT_SECONDS * 1_000,
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                ) as ai_client:
                    return ai_client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=CLASSIFY_PROMPT,
                            response_mime_type="application/json",
                            response_json_schema=INTENT_RESPONSE_SCHEMA,
                            temperature=0,
                            max_output_tokens=80,
                        ),
                    )

            response = await asyncio.wait_for(
                asyncio.to_thread(generate), timeout=GEMINI_TIMEOUT_SECONDS + 2
            )
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.text.strip())
            return _validate_intent(json.loads(raw))
        except Exception as exc:  # Gemini is optional and never authorizes writes.
            last_error = exc
    _log(f"WARN read-only command classifier unavailable: {type(last_error).__name__}")
    return _rule_based_read_only_intent(text)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def handle_set_amounts(
    symbol_value: str,
    low_value: Any,
    high_value: Any,
    message: discord.Message,
    *,
    mid_value: Any | None = None,
) -> None:
    """Atomically queue all regime budgets; edits require a disabled target."""

    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
        low = _parse_amount(low_value, "lower amount")
        high = _parse_amount(high_value, "higher amount")
        if low > high:
            raise ValueError("the lower amount must not exceed the higher amount")
        if mid_value is None:
            mid = amount_for_tier_gbp(
                {
                    "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": high},
                    "BUY_ENABLED": False,
                },
                "MID",
            )
        else:
            mid = _parse_amount(mid_value, "sideways amount")
        if low > mid or mid > high:
            raise ValueError("the amounts must satisfy low <= sideways <= high")
    except ValueError as exc:
        await message.reply(f"Invalid request: {_safe_text(exc)}")
        return

    raw = await asyncio.to_thread(get_repo_variable, RULES_VARIABLE)
    try:
        rules = validate_rules_map(raw or "")
    except ConfigError as exc:
        await message.reply(f"Blocked: live rules are invalid ({_safe_text(exc)}).")
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
        "mid_amount_gbp_json": json.dumps(mid, separators=(",", ":")),
        # This compatibility-named workflow input stores the upper endpoint;
        # it no longer means that an uptrend selects the amount.
        "up_amount_gbp_json": json.dumps(high, separators=(",", ":")),
    }
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        await message.reply(
            f"Queued atomic budgets for **{symbol}**: lower {_display_amount(low)}, "
            f"sideways {_display_amount(mid)}, higher "
            f"{_display_amount(high)}. Run `!dca analyze {symbol.split('_', 1)[0]}` "
            "after the workflow completes.\n" + _workflow_link("update_dca_config.yml")
        )
    else:
        await message.reply(_unconfirmed_dispatch("update_dca_config.yml"))


def _enable_targets(symbol: str) -> tuple[str, ...]:
    return tuple(ALLOWED_TARGETS) if symbol == "all" else (symbol,)


def _enable_review_revision(symbol: str) -> tuple[int, ...]:
    return tuple(_enable_review_revisions[target] for target in _enable_targets(symbol))


def _cancel_overlapping_enable_reviews(symbol: str) -> None:
    targets = set(_enable_targets(symbol))
    for target in targets:
        _enable_review_revisions[target] += 1
    for author_id, pending in list(_pending_enable_confirmations.items()):
        if targets.intersection(_enable_targets(pending["review"]["symbol"])):
            _pending_enable_confirmations.pop(author_id, None)


async def handle_disable(symbol_value: str, message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = "all" if symbol_value == "all" else _normalise_usd_key(symbol_value)
    except ValueError as exc:
        await message.reply(f"Invalid request: {_safe_text(exc)}")
        return
    # Do this before any await, even if GitHub later returns an ambiguous failure.
    _cancel_overlapping_enable_reviews(symbol)
    inputs = {
        "action": "set_enabled",
        "symbol": symbol,
        "enabled_json": "false",
    }
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        label = "all four targets" if symbol == "all" else symbol
        await message.reply(
            f"Queued disable for **{label}**. Overlapping enable reviews were cancelled. "
            "Once applied, the trader's live "
            "pre-submit check blocks a new order; an order already accepted by "
            "Kraken will still be reconciled. Budgets, trading modes, and scheduling "
            "are unchanged.\n" + _workflow_link("update_dca_config.yml")
        )
    else:
        await message.reply(
            "Overlapping enable reviews were cancelled locally. "
            + _unconfirmed_dispatch("update_dca_config.yml")
        )


def _enable_review(
    symbol: str,
    rules: Mapping[str, Any],
    execution: Mapping[str, Any],
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
    targets = _enable_targets(symbol)
    if all(rules[target]["BUY_ENABLED"] for target in targets):
        label = "All four targets are" if symbol == "all" else f"{symbol} is"
        raise ConfigError(f"{label} already enabled")
    reviewed_targets = {}
    for target in targets:
        rule = rules[target]
        tier_amounts = {
            "LOW": amount_for_tier_gbp(rule, "LOW"),
            "MID": amount_for_tier_gbp(rule, "MID"),
            "UP": amount_for_tier_gbp(rule, "HIGH"),
        }
        for tier, configured_amount in tier_amounts.items():
            amount = float(configured_amount)
            if not DCA_AMOUNT_MIN_GBP <= amount <= DCA_AMOUNT_MAX_GBP:
                raise ConfigError(
                    f"{target} {tier} must be between £{DCA_AMOUNT_MIN_GBP:g} "
                    f"and £{DCA_AMOUNT_MAX_GBP:,.0f} before enabling"
                )
        reviewed_targets[target] = {
            "low": float(tier_amounts["LOW"]),
            "mid": float(tier_amounts["MID"]),
            "high": float(tier_amounts["UP"]),
            "enabled": rule["BUY_ENABLED"],
        }

    global_hash = global_rules_pre_state_hash(rules)
    expected_hash = global_hash if symbol == "all" else rules_hash(symbol, rules[symbol])
    exposure_rules = deepcopy(dict(rules))
    for target in targets:
        exposure_rules[target]["BUY_ENABLED"] = True
    maximum_exposure = float(maximum_daily_exposure_gbp(exposure_rules))
    review = {
        "symbol": symbol,
        "targets": reviewed_targets,
        "rules_hash": expected_hash,
        "global_rules_hash": global_hash,
        "maximum_exposure": maximum_exposure,
    }
    if symbol != "all":
        review.update(reviewed_targets[symbol])
    return review


async def handle_enable(symbol_value: str, message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = "all" if symbol_value == "all" else _normalise_usd_key(symbol_value)
        revision = _enable_review_revision(symbol)
        raw_rules, raw_execution = await asyncio.gather(
            asyncio.to_thread(get_repo_variable, RULES_VARIABLE),
            asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        )
        if raw_rules is None or raw_execution is None:
            raise ConfigError("GitHub did not return rules and execution state")
        rules = validate_rules_map(raw_rules)
        execution = validate_execution_state(raw_execution)
        if revision != _enable_review_revision(symbol):
            raise ConfigError("a disable was requested during this review; review again")
        if symbol == "all" and all(rules[target]["BUY_ENABLED"] for target in ALLOWED_TARGETS):
            _pending_enable_confirmations.pop(_message_author_id(message), None)
            await message.reply(
                "All four targets are already enabled. No workflow queued. "
                "Trading modes and scheduling are unchanged; enablement alone "
                "does not authorize an order."
            )
            return
        review = _enable_review(
            symbol,
            rules,
            execution,
        )
    except (ValueError, ConfigError) as exc:
        await message.reply(f"Blocked: {_safe_text(exc)}.")
        return

    command = f"!dca confirm enable {symbol}"
    author_id = _message_author_id(message)
    _pending_enable_confirmations[author_id] = {
        "command": command,
        "review": review,
        "expires_at": monotonic() + ENABLE_CONFIRMATION_TTL_SECONDS,
    }
    if symbol == "all":
        lines = [
            "**Enable review — all four targets**",
            "UPTREND/LOW · SIDEWAYS/MID · DOWNTREND/HIGH budgets in GBP:",
        ]
        for target, target_review in review["targets"].items():
            flag = "ENABLED" if target_review["enabled"] else "DISABLED"
            lines.append(
                f"**{target.replace('_', '/')}** — currently {flag} → ENABLED\n"
                f"{_display_amount(target_review['low'])} / "
                f"{_display_amount(target_review['mid'])} / "
                f"{_display_amount(target_review['high'])}"
            )
        lines.extend([
            "Maximum aggregate daily exposure after enable: "
            f"**{_display_amount(review['maximum_exposure'])}**",
            "One atomic update: any invalid budget, Kraken minimum, changed rule, "
            "or pending order blocks the whole enable. All four prior decisions "
            "are invalidated before enabling.",
            "This does not change trading modes or scheduling and places no immediate order. "
            "After the workflow reports APPLIED, run `!dca analyze all`; buying "
            "still requires the next successful analysis and every execution check.",
            f"Send exactly `{command}` within {ENABLE_CONFIRMATION_TTL_SECONDS // 60} minutes.",
        ])
        await _reply_sections(message, ["\n".join(lines)])
        return
    await message.reply(
        f"**Enable review for {symbol}**\n"
        f"UPTREND/lower: {_display_amount(review['low'])} | "
        f"SIDEWAYS: {_display_amount(review['mid'])} | "
        f"DOWNTREND/higher: {_display_amount(review['high'])}\n"
        f"Maximum aggregate daily exposure after enable: "
        f"**{_display_amount(review['maximum_exposure'])}**\n"
        "Activation starts with the next successful analysis. Existing or stale "
        "decisions cannot trade after this enable. The serialized workflow will "
        "re-read the rules and check Kraken's current market minimum.\n"
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
    if monotonic() >= pending["expires_at"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply("Blocked: that enable confirmation expired; review again.")
        return
    if raw_text != pending["command"]:
        await message.reply(f"Blocked: send exactly `{pending['command']}`.")
        return

    expected = pending["review"]
    try:
        raw_rules, raw_execution = await asyncio.gather(
            asyncio.to_thread(get_repo_variable, RULES_VARIABLE),
            asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        )
        if raw_rules is None or raw_execution is None:
            raise ConfigError("GitHub did not return rules and execution state")
        rules = validate_rules_map(raw_rules)
        execution = validate_execution_state(raw_execution)
        current = _enable_review(
            expected["symbol"],
            rules,
            execution,
        )
    except ConfigError as exc:
        if _pending_enable_confirmations.get(author_id) is pending:
            _pending_enable_confirmations.pop(author_id, None)
        await message.reply(f"Blocked after live revalidation: {_safe_text(exc)}.")
        return

    if _pending_enable_confirmations.get(author_id) is not pending:
        await message.reply("Blocked: this review was replaced or already consumed. Review again.")
        return
    if monotonic() >= pending["expires_at"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply("Blocked: that enable confirmation expired during validation; review again.")
        return

    if current["global_rules_hash"] != expected["global_rules_hash"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(
            "Blocked: the global four-asset DCA rules changed after review. "
            "Run the enable command again to review current aggregate exposure."
        )
        return

    bound_fields = (
        "targets",
        "rules_hash",
        "maximum_exposure",
    )
    if any(current[field] != expected[field] for field in bound_fields):
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(
            "Blocked: budgets or aggregate exposure changed. "
            "Run the enable command again to review the live state."
        )
        return

    inputs = {
        "action": "set_enabled",
        "symbol": expected["symbol"],
        "enabled_json": "true",
        "expected_rules_hash": expected["rules_hash"],
        "expected_global_rules_hash": expected["global_rules_hash"],
    }
    _pending_enable_confirmations.pop(author_id, None)
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        if expected["symbol"] == "all":
            await message.reply(
                "Bulk enable validation queued for **all four targets** as one atomic "
                "update. No configuration result is confirmed yet. After the workflow "
                "reports APPLIED, run `!dca analyze all`; buying waits for the next "
                "successful analysis and every execution check. Trading modes and "
                "scheduling are unchanged; this places no immediate order.\n"
                + _workflow_link("update_dca_config.yml")
            )
            return
        await message.reply(
            f"Enable validation queued for **{expected['symbol']}**. It remains disabled "
            "unless the workflow confirms the same live rules and Kraken minimum. "
            "Once enabled, it waits for the next successful analysis before trading.\n"
            + _workflow_link("update_dca_config.yml")
        )
    else:
        await message.reply(_unconfirmed_dispatch("update_dca_config.yml"))


async def handle_analyze(params: dict[str, Any], message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: analysis requires an allowlisted Discord user.")
        return
    raw_symbol = str(params.get("symbol") or params.get("symbols") or "all").strip()
    if raw_symbol.lower() == "all":
        workflow_symbol = "all"
        label = "all four Kraken DCA targets"
    else:
        try:
            workflow_symbol = _to_usd_pair(raw_symbol)
        except ValueError as exc:
            await message.reply(f"Invalid request: {_safe_text(exc)}")
            return
        label = workflow_symbol
    inputs = {"symbol": workflow_symbol}
    if await asyncio.to_thread(trigger_workflow, "crypto_analysis.yml", inputs):
        await message.reply(
            f"Analysis queued for **{label}**. Deterministic Python selects regime, "
            "budget tier, and execution time; Gemini only explains the result.\n"
            + _workflow_link("crypto_analysis.yml")
        )
    else:
        await message.reply(_unconfirmed_dispatch("crypto_analysis.yml"))


async def handle_portfolio(params: dict[str, Any], message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: private reports require the configured channel and an allowlisted user.")
        return
    inputs = {"short_report": "true" if params.get("short_report", True) else "false"}
    if await asyncio.to_thread(trigger_workflow, "portfolio_check.yml", inputs):
        await message.reply("Read-only Kraken portfolio check queued.\n" + _workflow_link("portfolio_check.yml"))
    else:
        await message.reply(_unconfirmed_dispatch("portfolio_check.yml"))


def _history_data_through(decision: Mapping[str, Any]) -> str:
    """Render the last source candle timestamp without implying missing evidence."""

    history = decision.get("HISTORY")
    if not isinstance(history, Mapping):
        return "unknown"
    value = history.get("COVERAGE_THROUGH")
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    rendered = _local_timestamp(value)
    return "unknown" if rendered == "not scheduled" else rendered


def _history_last_traded_candle(decision: Mapping[str, Any]) -> str:
    history = decision.get("HISTORY")
    value = history.get("LAST_REAL_CANDLE_AT") if isinstance(history, Mapping) else None
    return _local_timestamp(value) if isinstance(value, str) and value else "unknown"


def _format_analysis_workflow_health(health: Mapping[str, Any]) -> str:
    """Format one concise line of GitHub workflow evidence for Discord."""

    status = str(health.get("status") or "UNKNOWN").upper()
    configured_ref = _safe_text(health.get("configured_ref") or "missing")
    actual_ref = health.get("actual_ref")
    head_sha = health.get("head_sha")
    actual = "unknown"
    if isinstance(actual_ref, str) and actual_ref:
        actual = _safe_text(actual_ref)
        if isinstance(head_sha, str) and head_sha:
            actual += f"@{_safe_text(head_sha[:12])}"
    run_state = _safe_text(health.get("run_status") or "unobserved")
    conclusion = health.get("conclusion")
    if conclusion:
        run_state = f"{run_state}/{_safe_text(conclusion)}"
    updated_at = health.get("updated_at")
    observed = _local_timestamp(updated_at) if isinstance(updated_at, str) else "unknown"
    line = (
        f"GitHub analysis workflow: **{status}** | configured ref: `{configured_ref}` | "
        f"actual ref: `{actual}` | run: `{run_state}` | observed: `{observed}`"
    )
    reason = health.get("reason")
    if isinstance(reason, str) and reason:
        line += f" | {_safe_text(reason)}"
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
    detail = _safe_text(health.get("detail") or "no watchdog evidence")
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


def _pair_label(symbol: str) -> str:
    """Render an internal target key as the market name the operator sees."""

    return symbol.replace("_", "/")


def get_trading_mode_health() -> dict[str, Any]:
    """Observe the execution authority separately from this Railway process."""

    mode = get_repo_variable("DCA_TRADING_MODE")
    mode = mode.strip().lower() if isinstance(mode, str) else "unknown"
    canary = get_repo_variable("DCA_CANARY_SYMBOL") if mode == "canary" else None
    canary = canary.strip().upper() if isinstance(canary, str) else None
    valid_modes = {"shadow", "canary", "live"}
    status = "MATCHED"
    if mode not in valid_modes or DCA_TRADING_MODE not in valid_modes:
        status = "UNKNOWN"
    elif mode != DCA_TRADING_MODE:
        status = "MISMATCH"
    elif mode == "canary" and (canary not in ALLOWED_TARGETS or canary != DCA_CANARY_SYMBOL):
        status = "MISMATCH" if canary in ALLOWED_TARGETS else "UNKNOWN"
    return {"status": status, "github_mode": mode, "canary_symbol": canary}


def _trading_mode_summary(mode_health: Mapping[str, Any] | None = None) -> str:
    health = mode_health or {"status": "UNKNOWN", "github_mode": "unknown"}
    github_mode = str(health.get("github_mode") or "unknown")
    line = (
        f"Trading modes: GitHub **{_safe_text(github_mode.upper())}** (order authority) | "
        f"Railway **{_safe_text(DCA_TRADING_MODE.upper())}**\n"
    )
    if health.get("status") != "MATCHED":
        return line + "⚠️ **MODE " + str(health.get("status", "UNKNOWN")) + " — buying readiness unverified.** Check both environments."
    if github_mode == "shadow":
        return line + "⏸️ **SHADOW — REAL KRAKEN ORDERS OFF**. Analysis can continue."
    if github_mode == "canary":
        return line + f"Canary target: **{_pair_label(str(health.get('canary_symbol')))}**; every execution check still applies."
    return line + "Live mode configured; this status is not order authorization. Every execution check still applies."


def _uptrend_override_summary(decision: Mapping[str, Any]) -> str:
    """Render an active emergency override from persisted analysis evidence."""

    signals = decision.get("SIGNALS")
    if not isinstance(signals, Mapping):
        return ""
    if signals.get("UPTREND_OVERRIDE_ACTIVE") is not True:
        return ""

    count = signals.get("UPTREND_CONFIRMATION_COUNT")
    required = signals.get("UPTREND_CONFIRMATION_REQUIRED")
    progress = (
        f"{count}/{required}"
        if type(count) is int and type(required) is int
        else "unknown"
    )
    natural = signals.get("REGIME_WITHOUT_OVERRIDE")
    if not isinstance(natural, str) or natural not in {
        "UPTREND",
        "DOWNTREND",
        "SIDEWAYS",
    }:
        natural = "unknown"
    activated_raw = signals.get("UPTREND_OVERRIDE_ACTIVATED_AT")
    activated = (
        _local_timestamp(activated_raw)
        if isinstance(activated_raw, str)
        else "unknown"
    )
    reason = signals.get("UPTREND_OVERRIDE_REASON")
    reason_text = "not recorded"
    if isinstance(reason, str) and reason.strip():
        reason_text = _safe_text(reason)
    return (
        "  🚨 **EMERGENCY UPTREND OVERRIDE ACTIVE** | "
        f"Rule result: `{natural}` | Confirmation: `{progress}` | "
        f"Activated: `{activated}` | Reason: {reason_text}"
    )


def _order_permission(
    symbol: str,
    *,
    enabled: bool,
    decision_ready: bool,
    decision_expired: bool,
    rules_match: bool,
    daily_analysis_pending: bool = False,
    mode_health: Mapping[str, Any] | None = None,
    global_history_ready: bool = False,
    pending_recovery: bool = False,
    bought_today: bool = False,
    execution_due: bool = False,
    start_date_blocked: bool = False,
) -> str:
    """Return the effective order posture for one pair in plain language."""

    if pending_recovery:
        return "⛔ RECOVERY ONLY — no new order; reconcile Kraken first"
    if bought_today:
        return "✅ ALREADY BOUGHT TODAY — daily limit reached"
    if not enabled:
        return "⛔ OFF — PAIR DISABLED"
    if start_date_blocked:
        return "⏳ WAITING — configured start date / first eligible analysis"
    if daily_analysis_pending:
        return "⏳ WAITING — TODAY'S ANALYSIS NOT DUE YET"
    if decision_expired:
        return "⏰ DONE FOR TODAY — NEXT ANALYSIS TOMORROW"
    if not decision_ready:
        return "⛔ BLOCKED — NO CURRENT DECISION"
    if not rules_match:
        return "🔄 WAITING FOR FRESH ANALYSIS"
    if not global_history_ready:
        return "⛔ BLOCKED — all four histories need current analysis"
    health = mode_health or {}
    if health.get("status") != "MATCHED":
        return "⚠️ UNVERIFIED — GitHub/Railway modes unavailable or mismatched"
    if health.get("github_mode") == "shadow":
        return "🟡 SIMULATION ONLY"
    if health.get("github_mode") == "canary" and symbol != health.get("canary_symbol"):
        return "🟡 SIMULATION ONLY — NOT CANARY"
    if not execution_due:
        return "⏳ WAITING FOR EXECUTION WINDOW — no order authorized here"
    return "🔎 DUE — trader must recheck quote, minimum, balance and live state"


def _decision_summary(
    symbol: str,
    rule: Mapping[str, Any],
    decision: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    now: datetime,
    daily_analysis_pending: bool = False,
    mode_health: Mapping[str, Any] | None = None,
    global_history_ready: bool = False,
) -> str:
    enabled = rule["BUY_ENABLED"]
    amounts = rule["REGIME_AMOUNTS_GBP"]
    configured_status = "ENABLED" if enabled else "DISABLED"
    configured_icon = "✅" if enabled else "⏸️"
    state_entry = execution.get(symbol, {})
    pending = isinstance(state_entry.get("PENDING_ORDER"), Mapping)
    pending_text = " | 🚨 KRAKEN RECOVERY PENDING" if pending else ""
    delivery_count = len(state_entry.get("PENDING_GIST_DELIVERIES", []))
    delivery_text = (
        f" | ⚠️ PORTFOLIO LEDGER DELIVERY WARNING ({delivery_count} pending)"
        if delivery_count
        else ""
    )
    history_data_through = _history_data_through(decision)
    current_date = now.astimezone(TIMEZONE).date().isoformat()
    decision_expired = (
        decision["ANALYSIS_STATUS"] == "READY"
        and (
            decision["ANALYSIS_DATE"] != current_date
            or parse_utc_iso(decision["VALID_UNTIL"]) < now
        )
    )
    decision_ready = (
        decision["ANALYSIS_STATUS"] == "READY"
        and decision["ANALYSIS_DATE"] == current_date
        and parse_utc_iso(decision["VALID_UNTIL"]) >= now
    )
    rules_match = (
        decision["RULES_HASH"] == rules_hash(symbol, rule)
        and decision.get("ENABLED") is enabled
    )
    permission = _order_permission(
        symbol,
        enabled=enabled,
        decision_ready=decision_ready,
        decision_expired=decision_expired,
        rules_match=rules_match,
        daily_analysis_pending=daily_analysis_pending,
        mode_health=mode_health,
        global_history_ready=global_history_ready,
        pending_recovery=any(isinstance(entry.get("PENDING_ORDER"), Mapping) for entry in execution.values()),
        bought_today=state_entry.get("LAST_BUY_DATE") == current_date,
        execution_due=decision_ready and is_execution_window(now, decision["EXECUTE_AT"], decision["VALID_UNTIL"]),
        start_date_blocked=(
            _schedule_start_date is not None and (
                now.astimezone(TIMEZONE).date() < _schedule_start_date
                or not decision_analyzed_on_or_after(decision, _schedule_start_date, TIMEZONE)
            )
        ),
    )
    if daily_analysis_pending:
        regime = decision["REGIME"]
        amount = _display_amount(effective_amount(rule, decision))
        next_time = _local_timestamp(decision["EXECUTE_AT"])
        selected_time = _local_timestamp(decision["SELECTED_AT"])
        age = _decision_age(decision, now)
        decision_status = "⏳ PRIOR DAY — awaiting 04:07 analysis"
    elif decision_ready:
        regime = decision["REGIME"]
        amount = _display_amount(effective_amount(rule, decision))
        next_time = _local_timestamp(decision["EXECUTE_AT"])
        selected_time = _local_timestamp(decision["SELECTED_AT"])
        age = _decision_age(decision, now)
        decision_status = (
            "✅ CURRENT"
            if rules_match
            else "🔄 REFRESH REQUIRED — budgets or enablement changed after this analysis"
        )
    elif decision_expired:
        regime = decision["REGIME"]
        amount = _display_amount(effective_amount(rule, decision))
        next_time = _local_timestamp(decision["EXECUTE_AT"])
        selected_time = _local_timestamp(decision["SELECTED_AT"])
        age = _decision_age(decision, now)
        decision_status = (
            "⏰ EXPIRED — no late order will be replayed; "
            "tomorrow's analysis resumes it"
        )
    else:
        regime = "ERROR"
        amount = "skipped"
        next_time = "not scheduled"
        age = _decision_age(decision, now)
        selected_time = "none"
        decision_status = f"❌ {decision['ANALYSIS_STATUS']}"
    override_summary = _uptrend_override_summary(decision)
    override_suffix = f"{override_summary}\n" if override_summary else ""
    next_step = ""
    if any(float(value) == 0 for value in amounts.values()):
        next_step = "\n  Next: approve and set LOW / MID / UP budgets while disabled; £0 cannot be enabled."
    elif not rules_match:
        next_step = f"\n  Next: run `!dca analyze {symbol.split('_')[0]}` after the configuration workflow succeeds."
    elif not decision_ready and not daily_analysis_pending and not decision_expired:
        next_step = "\n  Next: inspect the analysis workflow failure, then run `!dca analyze all`."
    error = decision.get("ERROR")
    error_text = f"\n  Analysis issue: {_safe_text(error)}" if error else ""
    return (
        f"{configured_icon} **{_pair_label(symbol)}** (`{symbol}`) | "
        f"**{configured_status}** | Orders: **{permission}**\n"
        f"  💷 UPTREND/lower "
        f"{_display_amount(amounts['LOW'])} | SIDEWAYS "
        f"{_display_amount(amount_for_tier_gbp(rule, 'MID'))} | DOWNTREND/higher "
        f"{_display_amount(amounts['UP'])}\n"
        f"  📊 {decision_status} | Regime: `{regime}` | Spend: {amount} | "
        f"Effective: `{next_time}`\n"
        f"{override_suffix}"
        f"  🧾 Coverage through: `{history_data_through}`\n"
        f"  Last traded candle: `{_history_last_traded_candle(decision)}` | "
        f"Last buy: `{_safe_text(state_entry.get('LAST_BUY_DATE') or 'never')}`"
        f"{pending_text}{delivery_text}{error_text}{next_step}"
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
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: private status requires the configured channel and an allowlisted user.")
        return
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await _reply_sections(message, [
            f"**DCA status: NOT READY**\nConfiguration/state validation failed: {_safe_text(exc)}\n"
            "Trading and scheduling fail closed."
        ], color=0xED4245)
        return

    ghostfolio_health, workflow_health, mode_health = await asyncio.gather(
        asyncio.to_thread(get_ghostfolio_delivery_health),
        asyncio.to_thread(get_analysis_workflow_health),
        asyncio.to_thread(get_trading_mode_health),
    )
    now = datetime.now(timezone.utc)
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    daily_analysis_pending = awaiting_daily_analysis_refresh(
        analysis, now, TIMEZONE
    )
    watchdog_health = _analysis_watchdog_health(
        analysis,
        now,
        workflow_health,
        awaiting_symbols=awaiting_symbols,
    )
    current_date = now.astimezone(TIMEZONE).date().isoformat()
    global_history_ready = all(
        decision["ANALYSIS_STATUS"] == "READY"
        and decision["ANALYSIS_DATE"] == current_date
        and decision.get("HISTORY", {}).get("STATUS") == "READY"
        for decision in analysis["TARGETS"].values()
    )
    refresh_symbols = [
        symbol
        for symbol in ALLOWED_TARGETS
        if rules[symbol]["BUY_ENABLED"]
        and analysis["TARGETS"][symbol]["ANALYSIS_STATUS"] == "READY"
        and analysis["TARGETS"][symbol]["ANALYSIS_DATE"] == current_date
        and analysis["TARGETS"][symbol]["RULES_HASH"]
        != rules_hash(symbol, rules[symbol])
    ]
    lines = [
        "🤖 **Kraken GBP-market DCA status (GBP budgets)**",
        f"Discord service: **{'CONNECTED' if client.is_ready() else 'CONNECTION UNVERIFIED'}** | Times: {TIMEZONE.key}",
        _trading_mode_summary(mode_health),
        f"All-four history gate: **{'READY' if global_history_ready else 'WAITING FOR DAILY ANALYSIS' if daily_analysis_pending else 'BLOCKED'}**",
        "⚙️ " + _format_analysis_workflow_health(workflow_health),
    ]
    if refresh_symbols:
        labels = ", ".join(_pair_label(symbol) for symbol in refresh_symbols)
        lines.append(
            "🔄 **Fresh analysis required:** "
            f"{labels} budgets changed after today's decisions. "
            "No order can use an old decision."
        )
    cards = []
    for symbol in ALLOWED_TARGETS:
        cards.append(
            _decision_summary(
                symbol,
                rules[symbol],
                analysis["TARGETS"][symbol],
                execution,
                now=now,
                daily_analysis_pending=daily_analysis_pending,
                mode_health=mode_health,
                global_history_ready=global_history_ready,
            )
        )
    all_disabled = not any(rule["BUY_ENABLED"] for rule in rules.values())
    if _schedule_error:
        scheduler = f"INVALID — {_safe_text(_schedule_error)}"
    elif _schedule_warning:
        scheduler = f"{'running' if DCA_CRON_ENABLED else 'paused'}; skipped target(s) — {_safe_text(_schedule_warning)}"
    elif not DCA_CRON_ENABLED:
        scheduler = "paused by DCA_CRON_ENABLED=false"
    elif daily_analysis_pending:
        scheduler = (
            "armed; awaiting 04:07 daily analysis for "
            f"{now.astimezone(TIMEZONE).date().isoformat()} {TIMEZONE.key}; "
            f"{len(_dca_schedule)} active target(s)"
        )
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
    displayed_chain_status = (
        "WAITING FOR FRESH ANALYSIS"
        if refresh_symbols
        and chain_status == "ATTENTION REQUIRED"
        and _schedule_error is None
        else chain_status
    )
    lines.append(f"⏱️ Railway scheduler: **{scheduler}**")
    lines.append("🛟 " + _format_analysis_watchdog_health(watchdog_health))
    if not DCA_CRON_ENABLED and _schedule_error is None:
        displayed_chain_status = "RAILWAY PAUSED — GitHub workflow health shown separately"
    lines.append(f"🔗 Analysis/scheduling chain: **{displayed_chain_status}**")
    analysis_ready = daily_analysis_pending or all(
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
            "⚠️ Portfolio ledger delivery: **WARNING — "
            f"{delivery_count} pending record(s); {retry_status}**"
        )
    else:
        lines.append("📒 Portfolio ledger delivery: **clear (0 pending records)**")
    if ghostfolio_health["status"] in {"CLEAR", "PENDING"}:
        lines.append(
            "👻 Local Ghostfolio completion: **"
            f"{ghostfolio_health['status'].lower()}** "
            f"({ghostfolio_health['completed']} receipt(s), "
            f"{ghostfolio_health['pending']} pending)"
        )
    else:
        lines.append(
            "👻 Local Ghostfolio completion: **"
            f"{ghostfolio_health['status'].lower()}**"
        )
    safe_paused = (
        all_disabled and not DCA_CRON_ENABLED
        and mode_health["status"] == "MATCHED"
        and mode_health["github_mode"] == "shadow"
    )
    if safe_paused:
        lines.append("⏸️ Trading posture: **INTENTIONALLY PAUSED** — all four targets disabled, shadow modes, Railway scheduling off.")
        if not global_history_ready or _schedule_error or _schedule_warning or pending_count or workflow_health["status"] not in {"HEALTHY", "ACTIVE"}:
            lines.append("⚠️ **Readiness needs attention independently of the safe pause.** Check the analysis, recovery and workflow details below.")
    elif (
        all_disabled
        and analysis_ready
        and pending_count == 0
        and chain_status == "OPERATIONAL"
        and mode_health["status"] == "MATCHED"
    ):
        lines.append("⏸️ Trading posture: **ready-but-disabled** (no target can submit a new order).")
    elif all_disabled:
        lines.append("⛔ Trading posture: **disabled and fail-closed; readiness needs attention**.")
    await _reply_sections(message, ["\n".join(lines), *cards], color=0xF0B232 if safe_paused else 0x5865F2)


async def handle_health(params: dict[str, Any], message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: private health requires the configured channel and an allowlisted user.")
        return
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await _reply_sections(message, [
            f"**DCA health: NOT READY**\n- State validation: FAILED ({_safe_text(exc)})\n"
            "- New orders: blocked"
        ], color=0xED4245)
        return
    ghostfolio_health, workflow_health, mode_health = await asyncio.gather(
        asyncio.to_thread(get_ghostfolio_delivery_health),
        asyncio.to_thread(get_analysis_workflow_health),
        asyncio.to_thread(get_trading_mode_health),
    )
    now = datetime.now(timezone.utc)
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    daily_analysis_pending = awaiting_daily_analysis_refresh(
        analysis, now, TIMEZONE
    )
    watchdog_health = _analysis_watchdog_health(
        analysis,
        now,
        workflow_health,
        awaiting_symbols=awaiting_symbols,
    )
    ready = []
    errors = []
    stale = []
    closed_windows = []
    mismatched = []
    for symbol in ALLOWED_TARGETS:
        decision = analysis["TARGETS"][symbol]
        if daily_analysis_pending or symbol in awaiting_symbols:
            continue
        if decision["ANALYSIS_STATUS"] != "READY":
            errors.append(symbol)
            continue
        if decision["ANALYSIS_DATE"] != now.astimezone(TIMEZONE).date().isoformat():
            stale.append(symbol)
            continue
        if decision["RULES_HASH"] != rules_hash(symbol, rules[symbol]) or decision.get("ENABLED") is not rules[symbol]["BUY_ENABLED"]:
            mismatched.append(symbol)
            continue
        if parse_utc_iso(decision["VALID_UNTIL"]) < now:
            # An elapsed same-day execution window is expected, not broken
            # analysis. The trader and each status card still prohibit replay.
            closed_windows.append(symbol)
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
        not errors
        and not stale
        and not mismatched
        and not awaiting_symbols
        and (not daily_analysis_pending or enabled_count == 0)
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
    awaiting_analysis = (
        enabled_count > 0
        and pending_count == 0
        and scheduler_ok
        and (bool(awaiting_symbols) or daily_analysis_pending)
    )
    posture = (
        "ARMED"
        if awaiting_analysis
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
    safe_paused = (
        enabled_count == 0 and not DCA_CRON_ENABLED
        and mode_health["status"] == "MATCHED"
        and mode_health["github_mode"] == "shadow"
    )
    faults = (
        not analysis_ok or pending_count or delivery_count
        or bool(_schedule_error) or bool(_schedule_warning)
        or workflow_health["status"] not in {"HEALTHY", "ACTIVE"}
    )
    if safe_paused:
        posture = "INTENTIONALLY PAUSED — ATTENTION REQUIRED" if faults else "INTENTIONALLY PAUSED"
    elif mode_health["status"] != "MATCHED":
        posture = "ATTENTION REQUIRED"
    lines = [
        f"**DCA health: {posture}**",
        f"- Discord service: {'CONNECTED' if client.is_ready() else 'CONNECTION UNVERIFIED'}",
        _trading_mode_summary(mode_health),
        f"- Rules: valid ({len(rules)}/{len(ALLOWED_TARGETS)} GBP targets; GBP budgets)",
        "- " + _format_analysis_workflow_health(workflow_health),
        "- " + _format_analysis_watchdog_health(watchdog_health),
        (
            (
                "- Analysis: awaiting 04:07 start-day analysis"
                if awaiting_symbols
                else "- Analysis: awaiting 04:07 daily analysis"
            )
            if awaiting_analysis or daily_analysis_pending
            else f"- Analysis: fresh READY {len(ready)}/{len(ALLOWED_TARGETS)}"
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
        f"- Analysis/scheduling chain: {'RAILWAY PAUSED — GitHub workflow health shown separately' if not DCA_CRON_ENABLED and _schedule_error is None else chain_status}",
        f"- Buy-enabled targets: {enabled_count}/{len(ALLOWED_TARGETS)}",
    ]
    if _schedule_start_date is not None:
        lines.append(
            f"- First permitted trade date: {_schedule_start_date.isoformat()} "
            f"{TIMEZONE.key}"
        )
    if errors and not awaiting_analysis:
        lines.append("- Analysis ERROR: " + ", ".join(errors))
    if stale and not awaiting_analysis:
        lines.append("- Stale decisions: " + ", ".join(stale))
    if mismatched and not awaiting_analysis:
        lines.append("- Rules mismatch: " + ", ".join(mismatched))
    if _schedule_error:
        lines.append(f"- Scheduler validation: FAILED ({_safe_text(_schedule_error)})")
    if _schedule_warning:
        lines.append(f"- Scheduler skipped target(s): {_safe_text(_schedule_warning)}")
    if errors or stale or mismatched:
        lines.append("- Next: inspect Crypto Analysis, then run `!dca analyze all`; never replay an old decision.")
    if closed_windows:
        lines.append("- Execution windows closed for today: " + ", ".join(closed_windows) + "; no late purchases will be replayed.")
    if pending_count:
        lines.append("- Next: allow Kraken reconciliation to finish; never clear a pending intent by hand.")
    await _reply_sections(message, ["\n".join(lines)], color=0xED4245 if "ATTENTION REQUIRED" in posture else 0xF0B232 if safe_paused else 0x5865F2)


HELP_TEXT = """🐙 **Kraken GBP-market DCA controls — clear command guide**
Markets: **BTC/GBP**, **ETH/GBP**, **SOL/GBP**, and **DOGE/GBP**. Budgets are in GBP.

📊 **Read only**
`show status` or `!dca status` — pair state, today’s analysis, times, and order permission
`!dca health` — scheduler, workflow, analysis, ledger, and Ghostfolio health
`show portfolio` or `!dca portfolio` — queue a read-only Kraken holdings report
`help`, `!help`, or `!dca help` — show this guide

🔎 **Run deterministic analysis** *(allowlisted user)*
`!dca analyze BTC` — replace BTC with ETH, SOL or DOGE
`!dca analyze all` — analyze all four pairs

💷 **Change a budget** *(disable the pair first)*
`!dca set BTC amounts to 5 low, 10 sideways, and 20 high`
Wait for the workflow's applied confirmation before the next change.
LOW ≤ MID ≤ UP; all three need approved nonzero budgets before enabling.

⏸️ **Disable or review-enable targets**
`!dca disable BTC`
`!dca enable BTC` — review first, then copy the exact confirmation returned
`!dca disable all` — one update; cancels everyone's outstanding enable reviews
`!dca enable all` — review all four budgets and flags, then `!dca confirm enable all`
Confirm within five minutes. Enablement starts with the next successful analysis.
Any invalid budget or pending order blocks bulk enable. After APPLIED, run
`!dca analyze all`. These commands do not change modes or scheduling and place no immediate order.

💬 **Chat with Gemini**
Talk normally for explanations or read-only requests. Natural language cannot
change budgets, run analysis, enable/disable a pair, or place an order.

🛡️ Use the configured channel and allowlisted account for private reads and changes.
Changes require exact lowercase `!dca ` commands. “Queued” is not “applied”.
GitHub shadow mode prevents real orders even when a pair is enabled.
"""


async def handle_help(params: dict[str, Any], message: discord.Message) -> None:
    await _reply_sections(message, [HELP_TEXT])


# ---------------------------------------------------------------------------
# Absolute-time Railway scheduler
# ---------------------------------------------------------------------------


def _clear_schedule(error: str | None = None) -> None:
    global _schedule_error, _schedule_warning, _schedule_start_date
    global _awaiting_daily_analysis
    _dca_schedule.clear()
    _pending_recovery_symbols.clear()
    _pending_gist_delivery_symbols.clear()
    _awaiting_start_day_symbols.clear()
    _awaiting_daily_analysis = False
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
    global _awaiting_daily_analysis
    _awaiting_start_day_symbols.clear()
    _awaiting_daily_analysis = False
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
    if awaiting_daily_analysis_refresh(analysis, current, TIMEZONE):
        _dca_schedule.clear()
        _schedule_error = None
        _schedule_warning = None
        _awaiting_daily_analysis = True
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
            "global all-four Kraken history gate: "
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
        if decision["RULES_HASH"] != rules_hash(symbol, rule) or decision.get("ENABLED") is not rule["BUY_ENABLED"]:
            invalid_enabled.append(f"{symbol}: rules mismatch")
            continue
        if parse_utc_iso(decision["VALID_UNTIL"]) < current.astimezone(timezone.utc):
            # A completed window is an expected terminal state, not a broken
            # scheduler. Status still shows the pair as done for today, while
            # tomorrow's analysis creates its next opportunity.
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
    if _awaiting_daily_analysis:
        return (
            "Scheduler armed; awaiting 04:07 daily analysis "
            f"until {START_DAY_ANALYSIS_EXPECTED_BY:%H:%M} {TIMEZONE.key}"
        )
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
        for part in _message_parts(_safe_text(content)):
            await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
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
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

READ_ONLY_ACTION_HANDLERS = {
    "portfolio": handle_portfolio,
    "status": handle_status,
    "help": handle_help,
    "health": handle_health,
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
    _log(
        f"INFO Gemini chat configured={bool(GEMINI_API_KEY)} "
        f"primary_model={AI_MODEL_CANDIDATES[0]} read_only=true"
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
_SET_EXPLICIT_AMOUNTS_RE = re.compile(
    r"^!dca set ([A-Za-z]+) amounts to "
    r"([0-9]+(?:\.[0-9]{1,2})?) low, "
    r"([0-9]+(?:\.[0-9]{1,2})?) sideways, and "
    r"([0-9]+(?:\.[0-9]{1,2})?) (?:high|up)$"
)
_DISABLE_RE = re.compile(r"^!dca disable ([A-Za-z]+)$")
_ENABLE_RE = re.compile(r"^!dca enable ([A-Za-z]+)$")
_ANALYZE_RE = re.compile(r"^!dca analyze (all|[A-Za-z]+)$")
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
    if text.startswith("!dca confirm"):
        await _handle_enable_confirmation(message, text)
        return True
    match = _SET_EXPLICIT_AMOUNTS_RE.fullmatch(text)
    if match:
        await handle_set_amounts(
            match.group(1),
            match.group(2),
            match.group(4),
            message,
            mid_value=match.group(3),
        )
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
            "🧭 Unrecognized exact DCA command; no changes were made. Type `help`; "
            "spelling and spacing are safety checks."
        )
        return True
    return False


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user:
        return
    if not _is_authorized_config_writer(message):
        return
    mentions = getattr(message, "mentions", [])

    text = str(message.content)
    for mention in mentions:
        text = text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    text = text.strip()
    if not text:
        await handle_help({}, message)
        return
    if await _handle_exact_dca_command(text, message):
        return
    if _COMMAND_LIKE_RE.search(text):
        await message.reply(
            "🧭 Command not accepted; no changes were made. Type `help` and copy "
            "an exact lowercase command."
        )
        return
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
        intent = await classify_intent(text)
    handler = READ_ONLY_ACTION_HANDLERS.get(intent["action"])
    if handler:
        await handler(intent.get("params", {}), message)
    elif intent["action"] == "chat":
        await message.reply(intent["reply"])
    else:
        await message.reply(
            "🤔 I didn’t understand that safely. Ask about the bot or type `help` "
            "for the exact commands."
        )


if __name__ == "__main__":
    required = (
        "DISCORD_BOT_TOKEN",
        "GH_PAT",
        "GITHUB_REPO",
        "GITHUB_WORKFLOW_REF",
        "DISCORD_CHANNEL_ID",
        "DISCORD_ALLOWED_USERS",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        _log("ERROR missing required environment variables: " + ", ".join(missing))
        sys.exit(1)
    _log(
        f"INFO starting Kraken GBP-market DCA Discord service cron_enabled={DCA_CRON_ENABLED}"
    )
    client.run(DISCORD_BOT_TOKEN, log_handler=None)

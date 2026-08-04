"""
Discord Bot for DCA Automation Control.

Listens for natural language commands in Discord and triggers
GitHub Actions workflows or queries repository variables via the GitHub API.

Setup:
    1. Create a Discord Application at https://discord.com/developers/applications
    2. Enable "Message Content Intent" under Bot settings
    3. Generate a Bot Token and invite the bot to your server with permissions:
       - Send Messages, Read Messages, Add Reactions
    4. Set the required environment variables (see below)
    5. pip install -r bot_requirements.txt
    6. python discord_bot.py

Required environment variables:
    DISCORD_BOT_TOKEN   - Discord bot token (from Discord Developer Portal)
    GEMINI_API_KEY      - Google AI Studio API key (for NL intent classification)
    GH_PAT              - GitHub Personal Access Token (repo scope)
    GITHUB_REPO         - GitHub repo in "owner/repo" format
    GITHUB_WORKFLOW_REF - Exact branch/tag used for workflow dispatches

Optional environment variables:
    DISCORD_CHANNEL_ID  - Restrict bot to one channel (responds to all messages there)
    DISCORD_ALLOWED_USERS - Comma-separated writer IDs; writes are blocked if omitted
    DCA_CRON_ENABLED    - "true" to enable built-in DCA scheduler (replaces cron-job.org)
    TIMEZONE            - Timezone for scheduler (default: Asia/Bangkok)
"""
import asyncio
import json
import math
import os
import re
import sys
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
import requests
from google import genai


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GH_PAT = os.environ.get("GH_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_WORKFLOW_REF = os.environ.get("GITHUB_WORKFLOW_REF", "").strip()

# Optional restrictions
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
ALLOWED_USERS = os.environ.get("DISCORD_ALLOWED_USERS", "")

# DCA Scheduler — replaces external cron-job.org polling
DCA_CRON_ENABLED = os.environ.get("DCA_CRON_ENABLED", "false").lower() == "true"
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))
DCA_AMOUNT_MIN_GBP = 5
DCA_AMOUNT_MAX_GBP = 1000
CONFIG_WRITE_PREFIX = "!dca "
ENABLE_CONFIRMATION_TTL_SECONDS = 300
DISPATCH_RETRY_SECONDS = 30 * 60
EXECUTION_STATE_VARIABLE = "DCA_EXECUTION_STATE"
CONFIG_WRITE_ACTIONS = {"analyze", "update_dca"}

# Enabling a target always requires a second, exact command from the same
# allowlisted Discord user. Pending confirmations are intentionally in-memory:
# a restart safely cancels them instead of enabling anything unexpectedly.
_pending_enable_confirmations: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Gemini setup — candidate models in order of preference (fast → fallback)
# ---------------------------------------------------------------------------

AI_MODEL_CANDIDATES = [
    "gemini-2.5-flash-lite",   # Optimized for speed/volume
    "gemini-2.5-flash",        # More capable Flash fallback
]

CLASSIFY_PROMPT = """You are a command classifier for a cryptocurrency DCA automation system.
Given a user message, classify the intent and extract parameters.

IMPORTANT: Users refer to coins by name only — "BTC", "LINK", "SUI", "ETH", "bitcoin", "chainlink", etc.
Never require a trading pair. All market analysis and trading use GBP.
Always derive the coin symbol from the name and convert it to the correct internal format.

Available actions:
1. "analyze" - Run crypto market analysis
   - symbols: comma-separated coin names exactly as the user said — e.g. "BTC, LINK, SUI" (default: derive from current DCA config)
     Accept plain names like "BTC", "bitcoin", "link", "chainlink" — do not add a quote currency here.
   - short_report: true for AI summary only, false for full breakdown (default: true)

2. "portfolio" - Check portfolio balance
   - short_report: true for balance/holdings only (no trade history) (default: true), false for full monthly report with 5th-to-5th trade history

3. "update_dca" - Update DCA configuration for a symbol
   - symbol: ALWAYS use the "COIN_GBP" format — e.g. "BTC_GBP", "LINK_GBP", "SUI_GBP".
     Convert any coin name or abbreviation the user mentions to this format:
     "btc" → "BTC_GBP", "bitcoin" → "BTC_GBP", "link" → "LINK_GBP", "chainlink" → "LINK_GBP",
     "doge" → "DOGE_GBP", "dogecoin" → "DOGE_GBP".
     Never output a non-GBP pair or a bare coin name like "BTC" — always append "_GBP".
   - field: one of "TIME", "AMOUNT_GBP", "BUY_ENABLED"
    - value: new value (HH:MM for TIME, number 5-1000 for AMOUNT_GBP, true/false for BUY_ENABLED)
   Note: "disable X" or "turn off X" means BUY_ENABLED=false; "enable X" or "turn on X" means BUY_ENABLED=true.

4. "status" - Show current DCA configuration

5. "accounts" - Show Ghostfolio portfolio account mapping

6. "help" - Show available commands

7. "unknown" - Message is not a recognized command

Respond with ONLY valid JSON, no markdown fences:
{"action": "...", "params": {...}, "reply": "Brief description of what will be done"}"""


# Valid actions the bot supports
VALID_ACTIONS = {"analyze", "portfolio", "status", "update_dca", "accounts", "help", "unknown"}


def _validate_intent(intent: dict) -> dict:
    """Validate and sanitize the AI-classified intent before use."""
    if not isinstance(intent, dict):
        return {"action": "unknown", "params": {}, "reply": ""}

    action = intent.get("action", "unknown")
    if not isinstance(action, str) or action not in VALID_ACTIONS:
        return {"action": "unknown", "params": {}, "reply": ""}

    params = intent.get("params", {})
    if not isinstance(params, dict):
        params = {}

    # For update_dca, enforce required param types from the AI
    if action == "update_dca":
        symbol = params.get("symbol")
        field = params.get("field")
        value = params.get("value")
        if not isinstance(symbol, str) or not symbol.strip():
            return {"action": "unknown", "params": {}, "reply": "Could not determine symbol"}
        if not isinstance(field, str) or not field.strip():
            return {"action": "unknown", "params": {}, "reply": "Could not determine field"}
        if value is None:
            return {"action": "unknown", "params": {}, "reply": "Could not determine value"}

    return {"action": action, "params": params, "reply": intent.get("reply", "")}


def _allowed_user_ids() -> set[str]:
    """Return the configured Discord writer allowlist."""
    return {user_id.strip() for user_id in ALLOWED_USERS.split(",") if user_id.strip()}


def _message_author_id(message: discord.Message) -> str:
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", "")
    return str(author_id).strip()


def _is_authorized_config_writer(message: discord.Message) -> bool:
    """Fail closed unless this message author is explicitly allowlisted."""
    allowed_ids = _allowed_user_ids()
    author_id = _message_author_id(message)
    return bool(allowed_ids and author_id and author_id in allowed_ids)


def _config_write_block_reason(
    action: str, raw_text: str, message: discord.Message
) -> str | None:
    """Return why a config-writing command must be refused, if applicable."""
    if action not in CONFIG_WRITE_ACTIONS:
        return None
    if not _is_authorized_config_writer(message):
        return (
            "Configuration changes require `DISCORD_ALLOWED_USERS` and an "
            "explicitly allowlisted Discord user."
        )
    if not raw_text.startswith(CONFIG_WRITE_PREFIX):
        return (
            f"Configuration-changing commands must start exactly with "
            f"`{CONFIG_WRITE_PREFIX}`."
        )
    return None


async def classify_intent(text: str) -> dict:
    """Use Gemini to classify user intent from natural language."""
    last_error = None
    prompt = f"{CLASSIFY_PROMPT}\n\nUser message: {text}"

    for model_name in AI_MODEL_CANDIDATES:
        try:
            def generate():
                with genai.Client(api_key=GEMINI_API_KEY) as ai_client:
                    return ai_client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )

            response = await asyncio.to_thread(
                generate,
            )
            raw = response.text.strip()
            # Strip markdown code fences if Gemini wraps them
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            result = _validate_intent(parsed)
            print(f"  AI model: {model_name} ✅")
            return result
        except Exception as e:
            last_error = e
            err_str = str(e).split("\n")[0]
            print(f"  AI model {model_name} failed: {err_str}")

    print(f"⚠️ All AI models failed. Last error: {last_error}")
    err_msg = str(last_error).split("\n")[0][:200]
    return {"action": "unknown", "params": {}, "reply": f"All AI models failed: {err_msg}"}


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

GH_HEADERS = {
    "Authorization": f"token {GH_PAT}",
    "Accept": "application/vnd.github.v3+json",
}
GH_API = "https://api.github.com"


def trigger_workflow(workflow_file: str, inputs: dict | None = None) -> bool:
    """Trigger a GitHub Actions workflow via the dispatch API. Returns True on success."""
    if not GITHUB_WORKFLOW_REF:
        print("❌ GITHUB_WORKFLOW_REF is required; refusing workflow dispatch")
        return False
    url = f"{GH_API}/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches"
    body = {"ref": GITHUB_WORKFLOW_REF}
    if inputs:
        body["inputs"] = inputs
    try:
        r = requests.post(url, json=body, headers=GH_HEADERS, timeout=10)
        return r.status_code == 204
    except Exception as e:
        print(f"❌ GitHub API error: {e}")
        return False


def get_repo_variable(name: str) -> str | None:
    """Fetch a GitHub Actions repository variable value."""
    url = f"{GH_API}/repos/{GITHUB_REPO}/actions/variables/{name}"
    try:
        r = requests.get(url, headers=GH_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json().get("value")
    except Exception as e:
        print(f"❌ GitHub API error: {e}")
    return None


# ---------------------------------------------------------------------------
# DCA Scheduler — smart cron replacement
# ---------------------------------------------------------------------------

# Maps "HH:MM" to enabled GBP symbols and their LAST_BUY_DATE values.
_dca_schedule: dict[str, dict] = {}

# A workflow dispatch is suppressed briefly, then retried unless live execution
# state confirms completion. HTTP 204 means accepted, not that the trade succeeded.
_dca_dispatch_guard: dict[tuple[str, str], float] = {}
_pending_recovery_symbols: set[str] = set()

def refresh_dca_schedule(
    raw_json: str | None, execution_state_json: str | None = None
) -> None:
    """Parse GBP rules plus trader-owned execution state for scheduling."""
    def clear_schedule() -> None:
        _dca_schedule.clear()
        _pending_recovery_symbols.clear()

    if not raw_json:
        clear_schedule()
        return
    try:
        target_map = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        clear_schedule()
        return
    try:
        execution_state = json.loads(execution_state_json or "{}")
    except (json.JSONDecodeError, ValueError):
        clear_schedule()
        return

    if not isinstance(target_map, dict) or not target_map:
        clear_schedule()
        return
    if not isinstance(execution_state, dict):
        clear_schedule()
        return

    def valid_date(value) -> bool:
        if value == "":
            return True
        if not isinstance(value, str):
            return False
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
        except ValueError:
            return False

    for symbol, config in target_map.items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+_GBP", symbol):
            clear_schedule()
            return
        if not isinstance(config, dict):
            clear_schedule()
            return
        if "AMOUNT" in config or "LAST_BUY_DATE" in config or "PENDING_ORDER" in config:
            clear_schedule()
            return
        enabled = config.get("BUY_ENABLED")
        amount = config.get("AMOUNT_GBP")
        target_time = config.get("TIME")
        if not isinstance(enabled, bool):
            clear_schedule()
            return
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < (DCA_AMOUNT_MIN_GBP if enabled else 0)
            or amount > DCA_AMOUNT_MAX_GBP
        ):
            clear_schedule()
            return
        if not isinstance(target_time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", target_time
        ):
            clear_schedule()
            return

    for symbol, state_entry in execution_state.items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+_GBP", symbol):
            clear_schedule()
            return
        if not isinstance(state_entry, dict) or not valid_date(
            state_entry.get("LAST_BUY_DATE", "")
        ):
            clear_schedule()
            return
        pending = state_entry.get("PENDING_ORDER")
        if pending is None:
            continue
        if not isinstance(pending, dict):
            clear_schedule()
            return
        pending_amount = pending.get("amount_gbp")
        if (
            not isinstance(pending.get("client_order_id"), str)
            or not re.fullmatch(r"dca-[0-9a-f]{14}", pending["client_order_id"])
            or not valid_date(pending.get("trade_date"))
            or isinstance(pending_amount, bool)
            or not isinstance(pending_amount, (int, float))
            or not math.isfinite(pending_amount)
            or not DCA_AMOUNT_MIN_GBP <= pending_amount <= DCA_AMOUNT_MAX_GBP
        ):
            clear_schedule()
            return

    # Collect enabled symbols grouped by their TIME, preserving LAST_BUY_DATE
    time_slots: dict[str, dict[str, str]] = {}
    for symbol, config in target_map.items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+_GBP", symbol):
            continue
        if not isinstance(config, dict):
            continue
        if not config.get("BUY_ENABLED", False):
            continue
        time_val = config.get("TIME", "")
        if not isinstance(time_val, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", time_val
        ):
            continue
        state_entry = execution_state.get(symbol, {})
        if not isinstance(state_entry, dict):
            continue
        time_slots.setdefault(time_val, {})[symbol] = state_entry.get(
            "LAST_BUY_DATE", ""
        )

    _dca_schedule.clear()
    _dca_schedule.update({
        time_val: {"symbols": symbols}
        for time_val, symbols in time_slots.items()
    })
    _pending_recovery_symbols.clear()
    _pending_recovery_symbols.update(
        symbol
        for symbol, state_entry in execution_state.items()
        if isinstance(symbol, str)
        and re.fullmatch(r"[A-Z0-9]+_GBP", symbol)
        and isinstance(state_entry, dict)
        and isinstance(state_entry.get("PENDING_ORDER"), dict)
    )


def _get_repo_variable_and_refresh(name: str) -> str | None:
    """Fetch one repository variable without mutating scheduler state."""
    return get_repo_variable(name)


def _format_cron_status() -> str:
    """Build a status line showing planned GHA dispatch times for all scheduled slots."""
    if not DCA_CRON_ENABLED or not _dca_schedule:
        return ""

    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d")
    current_min = now.hour * 60 + now.minute
    parts: list[str] = []

    for time_str in sorted(_dca_schedule):
        info = _dca_schedule[time_str]
        h, m = map(int, time_str.split(":"))
        target_min = h * 60 + m

        # Check if all symbols already bought today
        symbols_dict = info["symbols"]
        all_bought = all(lbd == today for lbd in symbols_dict.values())

        # Compute all aligned dispatch times in the -5/+60 min window, sorted by offset from target
        slots: list[tuple[int, int]] = []  # (diff, slot_min)
        for tick in range(0, 24 * 12):
            slot_min = tick * 5
            diff = slot_min - target_min
            if -5 <= diff <= 60:
                slots.append((diff, slot_min))
        slots.sort()

        dispatch_times: list[str] = []
        for diff, slot_min in slots:
            hh, mm = divmod(slot_min, 60)
            tag = f"{hh:02d}:{mm:02d}"
            slot_passed = current_min >= slot_min
            if all_bought or slot_passed:
                tag = f"~~{tag}~~"
            dispatch_times.append(tag)

        symbol_names = ", ".join(symbols_dict.keys())
        done = " ✅" if all_bought else ""
        parts.append(f"**{time_str}** ({symbol_names}){done}: {', '.join(dispatch_times)}")

    return "\n⏰ **Cron Dispatches**\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _symbols_from_dca_map() -> str:
    """Derive analysis symbols from DCA_TARGET_MAP on GitHub.

    Only canonical ``COIN_GBP`` keys are accepted. Invalid or old keys are
    skipped so they can never be silently reinterpreted as pound budgets.
    """
    raw = _get_repo_variable_and_refresh("DCA_TARGET_MAP")
    if not raw:
        raise ValueError("DCA_TARGET_MAP could not be loaded")
    try:
        target_map = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("DCA_TARGET_MAP is not valid JSON")
    if not isinstance(target_map, dict) or not target_map:
        raise ValueError("DCA_TARGET_MAP must be a non-empty object")

    symbols = []
    for key in target_map:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]+_GBP", key):
            raise ValueError("DCA_TARGET_MAP contains a non-GBP target key")
        symbols.append(f"{key[:-4]}/GBP")
    result = ", ".join(symbols)
    print(f"📋 Derived symbols from DCA_TARGET_MAP: {result}")
    return result


def _normalise_gbp_key(coin: str) -> str:
    """Return a canonical ``COIN_GBP`` key and reject non-GBP pairs."""
    FULL_NAMES: dict = {
        "bitcoin": "BTC", "ethereum": "ETH", "chainlink": "LINK",
        "solana": "SOL", "sui": "SUI", "cardano": "ADA", "ripple": "XRP",
        "dogecoin": "DOGE", "shiba": "SHIB", "polkadot": "DOT",
    }
    raw = coin.strip().lower()
    raw = FULL_NAMES.get(raw, raw).upper()
    normalized = raw.replace("/", "_")
    parts = normalized.split("_")
    if len(parts) > 2 or (len(parts) == 2 and parts[1] != "GBP"):
        raise ValueError("Only GBP trading pairs are supported")
    base = parts[0]
    if not re.fullmatch(r"[A-Z0-9]+", base):
        raise ValueError("Invalid coin symbol")
    return f"{base}_GBP"


def _to_gbp_pair(coin: str) -> str:
    """Return a canonical ``COIN/GBP`` analysis pair."""
    key = _normalise_gbp_key(coin)
    return f"{key[:-4]}/GBP"


async def handle_analyze(params: dict, message: discord.Message):
    """Trigger the crypto analysis workflow."""
    if not _is_authorized_config_writer(message):
        await message.reply(
            "⛔ Analysis can update DCA times and requires an explicitly "
            "allowlisted Discord user."
        )
        return

    symbols_raw = params.get("symbols", "") or ""
    short = params.get("short_report", True)

    if symbols_raw.strip():
        try:
            symbols = ", ".join(
                _to_gbp_pair(s)
                for s in re.split(r"[,\s]+", symbols_raw.strip())
                if s
            )
        except ValueError as error:
            await message.reply(f"❌ {error}")
            return
    else:
        # Fall back to deriving from the live DCA_TARGET_MAP
        try:
            symbols = _symbols_from_dca_map()
        except ValueError as error:
            await message.reply(f"❌ {error}")
            return

    inputs = {
        "symbol": str(symbols),
        "short_report": "true" if short else "false",
    }

    if trigger_workflow("crypto_analysis.yml", inputs):
        mode = "short" if short else "full"
        await message.reply(f"✅ Analysis triggered for **{symbols}** ({mode} report)")
    else:
        await message.reply("❌ Failed to trigger analysis workflow. Check bot logs.")


async def handle_portfolio(params: dict, message: discord.Message):
    """Trigger the portfolio balance check workflow."""
    short = params.get("short_report", True)

    inputs = {
        "short_report": "true" if short else "false",
    }

    if trigger_workflow("portfolio_check.yml", inputs):
        label = "short (balance only)" if short else "monthly"
        await message.reply(f"✅ Portfolio check triggered ({label} report)")
    else:
        await message.reply("❌ Failed to trigger portfolio workflow. Check bot logs.")


async def handle_status(params: dict, message: discord.Message):
    """Fetch and display the current DCA_TARGET_MAP configuration."""
    raw, execution_state_raw = await asyncio.gather(
        asyncio.to_thread(get_repo_variable, "DCA_TARGET_MAP"),
        asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
    )
    if not raw:
        await message.reply("❌ Could not fetch DCA_TARGET_MAP from GitHub")
        return

    try:
        target_map = json.loads(raw)
        execution_state = json.loads(execution_state_raw or "{}")
        if not isinstance(target_map, dict) or not isinstance(execution_state, dict):
            raise ValueError("configuration variables must be JSON objects")
    except (json.JSONDecodeError, ValueError):
        await message.reply("⚠️ DCA rules or execution state are malformed.")
        return

    lines = ["**📋 Current DCA Configuration**\n"]
    for symbol, config in target_map.items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+_GBP", symbol):
            lines.append(f"⚠️ **{symbol}** — unsupported config key; migrate it to `COIN_GBP`")
            continue
        if isinstance(config, dict):
            enabled = config.get("BUY_ENABLED", False)
            status = "🟢" if enabled else "🔴"
            state_entry = execution_state.get(symbol, {})
            if not isinstance(state_entry, dict):
                state_entry = {}
            pending_label = " — ⚠️ reconciliation pending" if isinstance(
                state_entry.get("PENDING_ORDER"), dict
            ) else ""
            lines.append(
                f"{status} **{symbol}** — "
                f"Time: `{config.get('TIME', '?')}`, "
                f"Amount: `£{config.get('AMOUNT_GBP', '?')}`, "
                f"Last Buy: `{state_entry.get('LAST_BUY_DATE') or 'never'}`"
                f"{pending_label}"
            )
        else:
            lines.append(f"⚠️ **{symbol}** — unsupported non-object configuration")

    cron_status = _format_cron_status()
    if cron_status:
        lines.append(cron_status)

    await message.reply("\n".join(lines))


async def handle_update_dca(
    params: dict,
    message: discord.Message,
    *,
    enable_confirmed: bool = False,
    confirmed_snapshot: dict | None = None,
):
    """Update a field in DCA_TARGET_MAP and save to GitHub."""
    if not _is_authorized_config_writer(message):
        await message.reply(
            "⛔ Configuration changes require `DISCORD_ALLOWED_USERS` and an "
            "explicitly allowlisted Discord user."
        )
        return
    if not enable_confirmed:
        _pending_enable_confirmations.pop(_message_author_id(message), None)

    symbol = str(params.get("symbol", "")).upper().strip()
    field = str(params.get("field", "")).upper()
    value = params.get("value")

    if not symbol or not field or value is None:
        await message.reply("❌ Missing required params: `symbol`, `field`, `value`")
        return

    try:
        symbol = _normalise_gbp_key(symbol)
    except ValueError as error:
        await message.reply(f"❌ {error}")
        return

    # "amount" is a convenient natural-language alias; the stored field is
    # deliberately explicit so a number can never be mistaken for another currency.
    if field == "AMOUNT":
        field = "AMOUNT_GBP"

    # Validate field
    allowed_fields = {"TIME", "AMOUNT_GBP", "BUY_ENABLED"}
    if field not in allowed_fields:
        await message.reply(f"❌ Can only update: {', '.join(sorted(allowed_fields))}")
        return

    # Validate and normalize value
    if field == "TIME":
        val_str = str(value)
        if not re.match(r"^\d{2}:\d{2}$", val_str):
            await message.reply("❌ TIME must be in HH:MM format (e.g., `23:00`)")
            return
        h, m = map(int, val_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            await message.reply("❌ TIME must be between 00:00 and 23:59")
            return

    elif field == "AMOUNT_GBP":
        try:
            value = float(value)
            if value < DCA_AMOUNT_MIN_GBP or value > DCA_AMOUNT_MAX_GBP:
                raise ValueError("out of range")
            if value == int(value):
                value = int(value)
        except (ValueError, TypeError):
            await message.reply(
                f"❌ AMOUNT_GBP must be between £{DCA_AMOUNT_MIN_GBP} and "
                f"£{DCA_AMOUNT_MAX_GBP}"
            )
            return

    elif field == "BUY_ENABLED":
        if str(value).lower() in ("true", "yes", "on", "1", "enable", "enabled"):
            value = True
        elif str(value).lower() in ("false", "no", "off", "0", "disable", "disabled"):
            value = False
        else:
            await message.reply("❌ BUY_ENABLED must be true or false")
            return

    # Fetch current map
    raw = _get_repo_variable_and_refresh("DCA_TARGET_MAP")
    if not raw:
        await message.reply("❌ Could not fetch current DCA_TARGET_MAP")
        return

    try:
        target_map = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await message.reply("❌ DCA_TARGET_MAP is malformed, cannot update safely")
        return

    # Verify symbol exists
    if symbol not in target_map:
        available = ", ".join(target_map.keys())
        await message.reply(f"❌ Symbol **{symbol}** not found. Available: {available}")
        return

    if not isinstance(target_map[symbol], dict):
        await message.reply(f"❌ Config for {symbol} is not in dict format, cannot update")
        return

    if field == "BUY_ENABLED" and value is True:
        try:
            configured_amount = float(target_map[symbol].get("AMOUNT_GBP"))
        except (TypeError, ValueError):
            configured_amount = 0
        if not DCA_AMOUNT_MIN_GBP <= configured_amount <= DCA_AMOUNT_MAX_GBP:
            await message.reply(
                f"⛔ Set AMOUNT_GBP between £{DCA_AMOUNT_MIN_GBP} and "
                f"£{DCA_AMOUNT_MAX_GBP} before enabling {symbol}."
            )
            return

        if not enable_confirmed:
            author_id = _message_author_id(message)
            confirmation_command = f"{CONFIG_WRITE_PREFIX}confirm enable {symbol}"
            _pending_enable_confirmations[author_id] = {
                "symbol": symbol,
                "command": confirmation_command,
                "amount_gbp": configured_amount,
                "time": target_map[symbol].get("TIME"),
                "expires_at": monotonic() + ENABLE_CONFIRMATION_TTL_SECONDS,
            }
            await message.reply(
                f"⚠️ Enabling **{symbol}** permits real Kraken orders. "
                f"To confirm, send exactly `{confirmation_command}` within "
                f"{ENABLE_CONFIRMATION_TTL_SECONDS // 60} minutes."
            )
            return

        if confirmed_snapshot is None or (
            configured_amount != confirmed_snapshot.get("amount_gbp")
            or target_map[symbol].get("TIME") != confirmed_snapshot.get("time")
        ):
            await message.reply(
                "⛔ The amount or time changed after confirmation was requested. "
                "Start the enable command again to review the current rules."
            )
            return

    # Queue the one-field update through the same GitHub concurrency group as
    # analysis and trading. Railway never PATCHes the shared rules map directly.
    old_value = target_map[symbol].get(field)
    inputs = {
        "symbol": symbol,
        "field": field,
        "value_json": json.dumps(value, separators=(",", ":")),
    }
    if field == "BUY_ENABLED" and value is True:
        inputs.update(
            {
                "expected_amount_gbp_json": json.dumps(
                    confirmed_snapshot["amount_gbp"], separators=(",", ":")
                ),
                "expected_time": str(confirmed_snapshot["time"]),
            }
        )
    if trigger_workflow("update_dca_config.yml", inputs):
        old_display = f"£{old_value}" if field == "AMOUNT_GBP" else str(old_value)
        new_display = f"£{value}" if field == "AMOUNT_GBP" else str(value)
        await message.reply(
            f"✅ Queued **{symbol}** → **{field}**: "
            f"`{old_display}` → `{new_display}`. "
            "GitHub will apply it against the latest rules in the serialized writer queue."
        )
    else:
        await message.reply("❌ Failed to queue the DCA configuration update")


async def _handle_enable_confirmation(
    message: discord.Message, raw_text: str
) -> None:
    """Apply only an exact, unexpired confirmation from the initiating user."""
    if not _is_authorized_config_writer(message):
        await message.reply(
            "⛔ Enable confirmations require an explicitly allowlisted Discord user."
        )
        return

    author_id = _message_author_id(message)
    pending = _pending_enable_confirmations.get(author_id)
    if not pending:
        await message.reply("⛔ No enable confirmation is pending for your user.")
        return
    if monotonic() > pending["expires_at"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply("⛔ That enable confirmation expired; start again.")
        return
    if raw_text != pending["command"]:
        await message.reply(
            f"⛔ Confirmation did not match. Send exactly `{pending['command']}`."
        )
        return

    symbol = pending["symbol"]
    confirmed_snapshot = {
        "amount_gbp": pending["amount_gbp"],
        "time": pending["time"],
    }
    _pending_enable_confirmations.pop(author_id, None)
    await handle_update_dca(
        {"symbol": symbol, "field": "BUY_ENABLED", "value": True},
        message,
        enable_confirmed=True,
        confirmed_snapshot=confirmed_snapshot,
    )


async def handle_accounts(params: dict, message: discord.Message):
    """Fetch and display the PORTFOLIO_ACCOUNT_MAP configuration."""
    raw = get_repo_variable("PORTFOLIO_ACCOUNT_MAP")
    if not raw:
        await message.reply("❌ Could not fetch PORTFOLIO_ACCOUNT_MAP from GitHub")
        return

    try:
        account_map = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await message.reply(f"⚠️ PORTFOLIO_ACCOUNT_MAP is malformed:\n```{raw[:500]}```")
        return

    lines = ["**🏦 Ghostfolio Account Mapping**\n"]
    for symbol, account_id in account_map.items():
        label = "(default fallback)" if symbol == "DEFAULT" else ""
        lines.append(f"• **{symbol}** → `{account_id}` {label}".rstrip())

    await message.reply("\n".join(lines))


HELP_TEXT = """**🤖 DCA Bot — Natural Language Commands**

**Analysis:**
• "!dca run analysis" / "!dca analyze BTC and LINK"
• "!dca full analysis for BTC/GBP" (detailed report)

**Portfolio:**
• "Check portfolio" / "Show my balance"
• "Monthly report" / "Full portfolio report"

**DCA Config:**
• "Show status" / "What's the current config?"
• "Show accounts" / "Portfolio account map"
• "!dca set BTC amount to 25 pounds"
• "!dca set BTC time to 22:00"
• "!dca disable LINK" / "!dca enable BTC"
✅ AMOUNT_GBP range: £5–£1000 per coin; Kraken's live market minimum is also checked
⛔ Config-changing commands require the exact `!dca ` prefix and an allowlisted user
⛔ Enabling a target requires the exact confirmation command returned by the bot

Read-only commands remain conversational; config writes require the safety prefix.
"""


async def handle_help(params: dict, message: discord.Message):
    """Show available commands."""
    await message.reply(HELP_TEXT)


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ---------------------------------------------------------------------------
# Scheduled tasks (DCA cron replacement)
# ---------------------------------------------------------------------------

# Clock-aligned times every five minutes. This covers every valid HH:MM target,
# including 23:51-23:59 without incorrectly wrapping it into the next day.
_FIVE_MINUTE_TICKS = [
    datetime.strptime(f"{h:02d}:{m:02d}", "%H:%M").time().replace(tzinfo=TIMEZONE)
    for h in range(24) for m in range(0, 60, 5)
]


def _due_symbols_for_dispatch(now: datetime) -> list[str]:
    """Return due or unresolved symbols outside the temporary dispatch cooldown."""
    today = now.strftime("%Y-%m-%d")
    current_min = now.hour * 60 + now.minute
    now_monotonic = monotonic()
    for entry, dispatched_at in list(_dca_dispatch_guard.items()):
        if entry[1] != today or now_monotonic - dispatched_at >= DISPATCH_RETRY_SECONDS:
            _dca_dispatch_guard.pop(entry, None)

    triggered_symbols: list[str] = [
        symbol
        for symbol in sorted(_pending_recovery_symbols)
        if (symbol, today) not in _dca_dispatch_guard
    ]
    for time_str, info in _dca_schedule.items():
        h, m = map(int, time_str.split(":"))
        target_min = h * 60 + m
        # Never wrap a late-night target into the next local day. The GitHub
        # quick check uses the same same-day arithmetic.
        diff = current_min - target_min

        # Align with the GitHub quick check: do not mark an early dispatch as
        # complete when the workflow would still decline to run the trader.
        if not -5 <= diff <= 60:
            continue

        for symbol, last_buy_date in info["symbols"].items():
            if last_buy_date == today:
                continue
            if symbol in triggered_symbols or (symbol, today) in _dca_dispatch_guard:
                continue
            triggered_symbols.append(symbol)

    return triggered_symbols


@tasks.loop(time=_FIVE_MINUTE_TICKS)
async def dca_scheduler_tick():
    """Dispatch due symbols, retrying until execution state confirms completion."""
    if not _dca_schedule and not _pending_recovery_symbols:
        return

    now = datetime.now(TIMEZONE)
    triggered_symbols = _due_symbols_for_dispatch(now)
    if not triggered_symbols:
        return

    success = await asyncio.to_thread(trigger_workflow, "daily_dca.yml")
    status = "✅" if success else "❌"
    symbols_str = ", ".join(triggered_symbols)
    print(f"{status} DCA cron dispatch for [{symbols_str}] at {now.strftime('%H:%M')}")
    if success:
        today = now.strftime("%Y-%m-%d")
        dispatched_at = monotonic()
        for symbol in triggered_symbols:
            _dca_dispatch_guard[(symbol, today)] = dispatched_at



@dca_scheduler_tick.before_loop
async def _before_scheduler_tick():
    await client.wait_until_ready()


async def _notify(content: str) -> None:
    """Send a plain message to DISCORD_CHANNEL_ID if configured."""
    if not CHANNEL_ID:
        return
    try:
        ch = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(int(CHANNEL_ID))
        await ch.send(content)
    except Exception as e:
        print(f"⚠️ _notify failed: {e}")


@tasks.loop(minutes=30)
async def dca_schedule_refresh():
    """Periodically refresh the DCA schedule from GitHub."""
    try:
        raw, execution_state_raw = await asyncio.gather(
            asyncio.to_thread(get_repo_variable, "DCA_TARGET_MAP"),
            asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        )
        if not raw:
            msg = "⚠️ DCA schedule refresh failed: GitHub returned no data for DCA_TARGET_MAP — schedule unchanged"
            print(msg)
            await _notify(msg)
            return
        old_times = set(_dca_schedule.keys())
        refresh_dca_schedule(raw, execution_state_raw or "{}")
        new_times = set(_dca_schedule.keys())
        if new_times != old_times:
            added = new_times - old_times
            removed = old_times - new_times
            lines = ["⏰ **DCA schedule updated**"]
            if removed:
                lines.append(f"  Removed: `{', '.join(sorted(removed))}`")
            if added:
                lines.append(f"  Added:   `{', '.join(sorted(added))}`")
            msg = "\n".join(lines)
            print(f"🔄 DCA schedule updated: {sorted(old_times)} → {sorted(new_times)}")
            await _notify(msg)
        else:
            times = ", ".join(sorted(_dca_schedule.keys()))
            print(f"🔄 DCA schedule refreshed (no change): {times}")
    except Exception as e:
        msg = f"❌ DCA schedule refresh error: {e}"
        print(msg)
        await _notify(msg)


@dca_schedule_refresh.before_loop
async def _before_schedule_refresh():
    await client.wait_until_ready()


ACTION_HANDLERS = {
    "analyze": handle_analyze,
    "portfolio": handle_portfolio,
    "status": handle_status,
    "update_dca": handle_update_dca,
    "accounts": handle_accounts,
    "help": handle_help,
}


@client.event
async def on_ready():
    """Log connection details on startup."""
    print(f"✅ Bot connected as {client.user} (ID: {client.user.id})")
    if CHANNEL_ID:
        print(f"📌 Restricted to channel ID: {CHANNEL_ID}")
    if ALLOWED_USERS:
        print(f"🔒 Allowed user IDs: {ALLOWED_USERS}")
    else:
        print("⚠️ No DISCORD_ALLOWED_USERS set — all configuration writes are blocked")

    # Start DCA scheduler if enabled
    if DCA_CRON_ENABLED:
        # Initial schedule load
        raw, execution_state_raw = await asyncio.gather(
            asyncio.to_thread(get_repo_variable, "DCA_TARGET_MAP"),
            asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        )
        refresh_dca_schedule(raw, execution_state_raw or "{}")
        if _dca_schedule:
            times = ", ".join(sorted(_dca_schedule.keys()))
            print(f"⏰ DCA scheduler loaded: {times}")
        else:
            print("⏰ DCA scheduler enabled but no active targets found")
        if not dca_scheduler_tick.is_running():
            dca_scheduler_tick.start()
        if not dca_schedule_refresh.is_running():
            dca_schedule_refresh.start()
        print(f"⏰ DCA scheduler started (-5/+60 min window, 5 min ticks, TZ={TIMEZONE})")


@client.event
async def on_message(message: discord.Message):
    """Process incoming messages and dispatch to action handlers."""
    # Ignore own messages
    if message.author == client.user:
        return

    # Channel restriction: if set, only respond in that channel
    if CHANNEL_ID and str(message.channel.id) != CHANNEL_ID:
        return

    # User restriction: if set, only allow listed users
    if ALLOWED_USERS:
        allowed_ids = [u.strip() for u in ALLOWED_USERS.split(",")]
        if str(message.author.id) not in allowed_ids:
            return

    # If no channel restriction, only respond to @mentions or DMs
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = client.user in message.mentions
    if not CHANNEL_ID and not is_dm and not is_mentioned:
        return

    # Clean the message text (strip bot mention)
    text = message.content
    for mention in message.mentions:
        text = text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    text = text.strip()

    if not text:
        await message.reply(HELP_TEXT)
        return

    # Exact enable confirmations bypass AI classification entirely.
    if text.startswith(f"{CONFIG_WRITE_PREFIX}confirm"):
        await _handle_enable_confirmation(message, text)
        return

    has_write_prefix = text.startswith(CONFIG_WRITE_PREFIX)
    classification_text = (
        text[len(CONFIG_WRITE_PREFIX) :].strip() if has_write_prefix else text
    )
    if has_write_prefix and not classification_text:
        await message.reply("❌ Add a configuration command after the exact `!dca ` prefix.")
        return

    # Classify intent via Gemini (show typing indicator while processing)
    async with message.channel.typing():
        intent = await classify_intent(classification_text)

    action = intent.get("action", "unknown")
    params = intent.get("params", {})

    print(f"[{message.author}] {text} → action={action} params={params}")

    handler = ACTION_HANDLERS.get(action)
    if handler:
        block_reason = _config_write_block_reason(action, text, message)
        if block_reason:
            await message.reply(f"⛔ {block_reason}")
            return
        await handler(params, message)
    elif action == "unknown":
        reply = intent.get("reply", "")
        if reply:
            # Truncate to stay under Discord's 2000-char limit
            reply = reply[:300]
            await message.reply(f"❓ I didn't understand that: *{reply}*\nType **help** to see available commands.")
        else:
            await message.reply("❓ I didn't understand that. Type **help** to see available commands.")
    else:
        await message.reply("❓ I didn't understand that. Type **help** to see available commands.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    missing = [v for v in (
        "DISCORD_BOT_TOKEN",
        "GEMINI_API_KEY",
        "GH_PAT",
        "GITHUB_REPO",
        "GITHUB_WORKFLOW_REF",
    )
               if not os.environ.get(v)]
    if not GITHUB_WORKFLOW_REF and "GITHUB_WORKFLOW_REF" not in missing:
        missing.append("GITHUB_WORKFLOW_REF")
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("\nRequired:")
        print("  DISCORD_BOT_TOKEN   - Discord bot token")
        print("  GEMINI_API_KEY      - Google AI Studio API key")
        print("  GH_PAT             - GitHub PAT with repo scope")
        print("  GITHUB_REPO        - owner/repo format")
        print("  GITHUB_WORKFLOW_REF - exact branch or tag for workflow dispatches")
        print("\nOptional:")
        print("  DISCORD_CHANNEL_ID  - Restrict to one channel")
        print("  DISCORD_ALLOWED_USERS - Comma-separated Discord user IDs")
        sys.exit(1)

    print("🚀 Starting DCA Discord Bot...")
    client.run(DISCORD_BOT_TOKEN)

"""Execute scheduled, GBP-denominated spot purchases on Kraken."""

import json
import math
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from gist_logger import update_gist_log
from kraken_client import (
    KrakenOrderNoFill,
    KrakenPreSubmissionError,
    KrakenOrderStateUnknown,
    build_client_order_id,
    place_market_buy,
    to_kraken_symbol,
)


TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)
DYNAMIC_DCA_DEFAULT_THRESHOLD_PERCENT = -2.0
DYNAMIC_DCA_DEFAULT_REDUCED_MULTIPLIER = 0.5
MIN_DCA_GBP = 5.0
MAX_DCA_GBP = 1000.0
DCA_TARGET_MAP_JSON = os.environ.get("DCA_TARGET_MAP", "{}")
DCA_EXECUTION_STATE_JSON = os.environ.get("DCA_EXECUTION_STATE", "{}")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

_CONFIG_KEY_PATTERN = re.compile(r"^[A-Z0-9]+_GBP$")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _gha_mask(value: str) -> None:
    """Mask a value in GitHub Actions logs."""
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def send_discord_alert(message, is_error=False):
    if not DISCORD_WEBHOOK_URL:
        return

    payload = {
        "embeds": [
            {
                "title": "Kraken GBP DCA Execution",
                "description": message,
                "color": 16711680 if is_error else 65280,
                "timestamp": datetime.now(SELECTED_TZ).isoformat(),
            }
        ]
    }
    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL, json=payload, timeout=5
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Failed to send Discord alert: {error}")


def _validate_config_key(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ValueError("DCA target keys must be strings such as BTC_GBP")
    key = symbol.strip()
    if not _CONFIG_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            f"Unsupported DCA target {symbol!r}; use a Kraken GBP key such as BTC_GBP"
        )
    return key


def _parse_amount_gbp(value, *, disabled=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("AMOUNT_GBP must be a JSON number")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("AMOUNT_GBP must be a finite, non-negative value")
    if disabled and amount <= MAX_DCA_GBP:
        return amount
    if amount < MIN_DCA_GBP or amount > MAX_DCA_GBP:
        raise ValueError(
            f"AMOUNT_GBP must be between GBP {MIN_DCA_GBP:.0f} and "
            f"GBP {MAX_DCA_GBP:.0f}"
        )
    return amount


def _parse_target_time(value, key: str) -> str:
    if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
        raise ValueError(f"{key}.TIME must use 24-hour HH:MM")
    return value


def _parse_last_buy_date(value, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key}.LAST_BUY_DATE must be empty or YYYY-MM-DD")
    if not value:
        return value
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            f"{key}.LAST_BUY_DATE must be empty or a valid YYYY-MM-DD date"
        ) from error
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"{key}.LAST_BUY_DATE must use YYYY-MM-DD")
    return value


def _execution_state_for_symbol(symbol_key, execution_state):
    if execution_state is None:
        return {}
    if not isinstance(execution_state, dict):
        raise ValueError("DCA_EXECUTION_STATE must be a JSON object")
    entry = execution_state.get(symbol_key, {})
    if not isinstance(entry, dict):
        raise ValueError(f"DCA_EXECUTION_STATE.{symbol_key} must be an object")
    return entry


def get_config_for_symbol(symbol_gbp, target_map, execution_state=None):
    """Return one validated GBP-native target configuration."""
    key = _validate_config_key(symbol_gbp)
    if not isinstance(target_map, dict) or key not in target_map:
        raise ValueError(f"No DCA configuration exists for {key}")

    entry = target_map[key]
    if not isinstance(entry, dict):
        raise ValueError(f"{key} must use the object configuration format")
    if "AMOUNT" in entry:
        raise ValueError(f"{key} uses unsupported AMOUNT; rename it to AMOUNT_GBP")
    execution_only_fields = {"LAST_BUY_DATE", PENDING_ORDER_FIELD}.intersection(entry)
    if execution_only_fields:
        fields = ", ".join(sorted(execution_only_fields))
        raise ValueError(
            f"{key} contains execution-only field(s) {fields}; move them to "
            f"{EXECUTION_STATE_VARIABLE}"
        )
    if "AMOUNT_GBP" not in entry:
        raise ValueError(f"{key} must define AMOUNT_GBP")
    for required_field in ("TIME", "BUY_ENABLED"):
        if required_field not in entry:
            raise ValueError(f"{key} must define {required_field}")

    buy_enabled = entry["BUY_ENABLED"]
    if not isinstance(buy_enabled, bool):
        raise ValueError(f"{key}.BUY_ENABLED must be true or false")

    amount_gbp = _parse_amount_gbp(entry["AMOUNT_GBP"], disabled=not buy_enabled)
    target_time = _parse_target_time(entry["TIME"], key)
    state_entry = _execution_state_for_symbol(key, execution_state)
    last_buy_date = _parse_last_buy_date(
        state_entry.get("LAST_BUY_DATE", ""), key
    )

    return {
        "TIME": target_time,
        "AMOUNT_GBP": amount_gbp,
        "BUY_ENABLED": buy_enabled,
        "LAST_BUY_DATE": last_buy_date,
        "DYNAMIC_DCA": entry.get("DYNAMIC_DCA"),
        "KEY": key,
    }


def get_dynamic_dca_settings(dynamic_dca):
    """Return validated per-asset dynamic DCA settings."""
    settings = {
        "enabled": False,
        "threshold_percent": DYNAMIC_DCA_DEFAULT_THRESHOLD_PERCENT,
        "reduced_multiplier": DYNAMIC_DCA_DEFAULT_REDUCED_MULTIPLIER,
        "error": None,
    }

    if dynamic_dca is None:
        return settings
    if not isinstance(dynamic_dca, dict):
        settings["error"] = "DYNAMIC_DCA must be an object."
        return settings

    enabled = dynamic_dca.get("ENABLED", False)
    if not isinstance(enabled, bool):
        settings["error"] = "DYNAMIC_DCA.ENABLED must be true or false."
        return settings

    try:
        threshold_percent = float(
            dynamic_dca.get(
                "THRESHOLD_PERCENT", DYNAMIC_DCA_DEFAULT_THRESHOLD_PERCENT
            )
        )
        reduced_multiplier = float(
            dynamic_dca.get(
                "REDUCED_MULTIPLIER", DYNAMIC_DCA_DEFAULT_REDUCED_MULTIPLIER
            )
        )
    except (TypeError, ValueError):
        settings["error"] = "DYNAMIC_DCA threshold and multiplier must be numeric."
        return settings

    if not math.isfinite(threshold_percent) or not math.isfinite(reduced_multiplier):
        settings["error"] = "DYNAMIC_DCA threshold and multiplier must be finite."
        return settings
    if not 0 < reduced_multiplier <= 1:
        settings["error"] = (
            "DYNAMIC_DCA.REDUCED_MULTIPLIER must be above 0 and at most 1."
        )
        return settings

    settings.update(
        {
            "enabled": enabled,
            "threshold_percent": threshold_percent,
            "reduced_multiplier": reduced_multiplier,
        }
    )
    return settings


def get_ghostfolio_account_id(symbol):
    """Return the configured Ghostfolio account for an asset, if available."""
    try:
        from portfolio_logger import get_account_id

        portfolio_map = json.loads(os.environ.get("PORTFOLIO_ACCOUNT_MAP", "{}"))
        if not isinstance(portfolio_map, dict):
            raise ValueError("PORTFOLIO_ACCOUNT_MAP must be a JSON object")
        return get_account_id(symbol, portfolio_map)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        print(f"Could not read PORTFOLIO_ACCOUNT_MAP: {error}")
    except Exception as error:
        print(f"Could not resolve Ghostfolio account for {symbol}: {error}")
    return None


def _build_dca_decision(amount_gbp, multiplier, roi_percent, reason):
    return {
        "amount_gbp": amount_gbp,
        "multiplier": multiplier,
        "roi_percent": roi_percent,
        "reason": reason,
    }


def determine_dynamic_dca_decision(symbol, configured_amount_gbp, dynamic_dca):
    """Choose a full or reduced GBP amount using the asset's Ghostfolio ROI."""
    configured_amount_gbp = _parse_amount_gbp(configured_amount_gbp)
    settings = get_dynamic_dca_settings(dynamic_dca)

    if settings["error"]:
        return _build_dca_decision(
            configured_amount_gbp,
            1.0,
            None,
            f"Full buy (x1): {settings['error']}",
        )
    if not settings["enabled"]:
        return _build_dca_decision(
            configured_amount_gbp,
            1.0,
            None,
            "Full buy (x1): Dynamic DCA is disabled.",
        )

    exchange_pair = to_kraken_symbol(symbol)
    base_symbol = exchange_pair.split("/", maxsplit=1)[0]
    account_id = get_ghostfolio_account_id(base_symbol)
    if not account_id:
        return _build_dca_decision(
            configured_amount_gbp,
            1.0,
            None,
            "Full buy (x1): Ghostfolio ROI is unavailable; using the configured amount.",
        )

    try:
        from portfolio_logger import get_asset_roi_percent

        roi_percent = get_asset_roi_percent(
            base_symbol, account_id, exchange_pair=exchange_pair
        )
    except Exception as error:
        print(f"Ghostfolio asset ROI lookup failed for {symbol}: {error}")
        roi_percent = None

    if roi_percent is None:
        return _build_dca_decision(
            configured_amount_gbp,
            1.0,
            None,
            "Full buy (x1): Ghostfolio ROI is unavailable; using the configured amount.",
        )

    if roi_percent >= settings["threshold_percent"]:
        reduced_amount = round(
            configured_amount_gbp * settings["reduced_multiplier"], 2
        )
        if reduced_amount >= MIN_DCA_GBP:
            label = (
                "Half buy"
                if settings["reduced_multiplier"] == 0.5
                else "Reduced buy"
            )
            return _build_dca_decision(
                reduced_amount,
                settings["reduced_multiplier"],
                roi_percent,
                (
                    f"{label} (x{settings['reduced_multiplier']:g}): asset ROI "
                    f"{roi_percent:+.2f}% is at or above "
                    f"{settings['threshold_percent']:.2f}%."
                ),
            )

        return _build_dca_decision(
            configured_amount_gbp,
            1.0,
            roi_percent,
            (
                "Full buy (x1): the reduced amount would be below the "
                f"GBP {MIN_DCA_GBP:.0f} execution guardrail."
            ),
        )

    return _build_dca_decision(
        configured_amount_gbp,
        1.0,
        roi_percent,
        (
            f"Full buy (x1): asset ROI {roi_percent:+.2f}% is below "
            f"{settings['threshold_percent']:.2f}%."
        ),
    )


def format_asset_roi(roi_percent):
    if roi_percent is None:
        return "Unavailable"
    return f"{roi_percent:+.2f}%"


def is_time_to_trade(target_time_str):
    """Return true once today's target time has arrived, with catch-up support."""
    now = datetime.now(SELECTED_TZ)
    try:
        target_hour, target_minute = map(int, target_time_str.split(":"))
        target_dt = now.replace(
            hour=target_hour,
            minute=target_minute,
            second=0,
            microsecond=0,
        )
    except (ValueError, AttributeError) as error:
        print(f"Invalid target time format: {target_time_str} ({error})")
        return False

    difference_seconds = (now - target_dt).total_seconds()
    if abs(difference_seconds) <= 300:
        print(f"Within target window. Diff={difference_seconds:.0f}s")
        return True
    if difference_seconds > 0:
        print(f"Target time passed today. Diff={difference_seconds:.0f}s; catch-up mode.")
        return True
    return False


EXECUTION_STATE_VARIABLE = "DCA_EXECUTION_STATE"
PENDING_ORDER_FIELD = "PENDING_ORDER"
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^dca-[0-9a-f]{14}$")


def _github_variable_context(variable_name):
    token = os.environ.get("GIST_TOKEN")
    if not token:
        raise RuntimeError("GIST_TOKEN is required to read live DCA configuration")

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
    url, collection_url, headers = _github_variable_context(variable_name)
    data = {
        "name": variable_name,
        "value": json.dumps(value, separators=(",", ":")),
    }
    if exists:
        response = requests.patch(url, headers=headers, json=data, timeout=15)
        expected_status = 204
    else:
        response = requests.post(
            collection_url, headers=headers, json=data, timeout=15
        )
        expected_status = 201
    if response.status_code != expected_status:
        operation = "update" if exists else "create"
        raise RuntimeError(
            f"{variable_name} {operation} failed with HTTP {response.status_code}"
        )


def fetch_live_target_map():
    """Fetch the current repository-wide map; never trade from a stale snapshot."""
    target_map, _exists = _fetch_repo_json_variable(
        "DCA_TARGET_MAP", required=True
    )
    return target_map


def _validate_execution_state(execution_state):
    if not isinstance(execution_state, dict):
        raise ValueError("DCA_EXECUTION_STATE must be a JSON object")
    for raw_key, entry in execution_state.items():
        key = _validate_config_key(raw_key)
        if not isinstance(entry, dict):
            raise ValueError(f"DCA_EXECUTION_STATE.{key} must be an object")
        _parse_last_buy_date(entry.get("LAST_BUY_DATE", ""), key)
        pending = entry.get(PENDING_ORDER_FIELD)
        if pending is None:
            continue
        if not isinstance(pending, dict):
            raise ValueError(f"{key}.{PENDING_ORDER_FIELD} must be an object")
        client_order_id = pending.get("client_order_id")
        if not isinstance(client_order_id, str) or not _CLIENT_ORDER_ID_PATTERN.fullmatch(
            client_order_id
        ):
            raise ValueError(f"{key} has an invalid pending client order ID")
        _parse_last_buy_date(pending.get("trade_date"), key)
        _parse_amount_gbp(pending.get("amount_gbp"))
    return execution_state


def fetch_live_execution_state():
    state, _exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=False
    )
    return _validate_execution_state(state)


def _pending_order_for_symbol(execution_state, symbol_key):
    entry = _execution_state_for_symbol(symbol_key, execution_state)
    return entry.get(PENDING_ORDER_FIELD)


def prepare_order_intent(symbol_key, client_order_id, trade_date, amount_gbp):
    """Persist an order intent before Kraken can receive a create request."""
    key = _validate_config_key(symbol_key)
    _parse_last_buy_date(trade_date, key)
    amount_gbp = _parse_amount_gbp(amount_gbp)
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id):
        raise ValueError("Invalid deterministic client order ID")

    state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=False
    )
    _validate_execution_state(state)
    entry = state.setdefault(key, {"LAST_BUY_DATE": ""})
    pending = entry.get(PENDING_ORDER_FIELD)
    if pending is not None:
        return pending, True

    pending = {
        "client_order_id": client_order_id,
        "trade_date": trade_date,
        "amount_gbp": amount_gbp,
    }
    entry[PENDING_ORDER_FIELD] = pending
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(f"Persisted durable Kraken order intent for {key} ({client_order_id}).")
    return pending, False


def clear_order_intent(symbol_key, client_order_id):
    """Clear a known-safe intent after a pre-submit or terminal no-fill failure."""
    key = _validate_config_key(symbol_key)
    state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=True
    )
    _validate_execution_state(state)
    pending = _pending_order_for_symbol(state, key)
    if pending is None:
        return
    if pending["client_order_id"] != client_order_id:
        raise RuntimeError(f"Refusing to clear a different pending order for {key}")
    state[key].pop(PENDING_ORDER_FIELD, None)
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(f"Cleared safe no-fill order intent for {key}.")


def complete_order_intent(symbol_key, client_order_id, completed_date):
    """Atomically record the daily completion and remove its durable intent."""
    key = _validate_config_key(symbol_key)
    _parse_last_buy_date(completed_date, key)
    state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=True
    )
    _validate_execution_state(state)
    pending = _pending_order_for_symbol(state, key)
    if pending is None or pending["client_order_id"] != client_order_id:
        raise RuntimeError(f"Durable order intent changed or disappeared for {key}")
    state[key]["LAST_BUY_DATE"] = completed_date
    state[key].pop(PENDING_ORDER_FIELD, None)
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(f"Completed order intent for {key}; LAST_BUY_DATE={completed_date}.")


def _trade_rule_snapshot(config):
    """Return the fields that must not change between decision and submission."""
    return (
        config["TIME"],
        config["AMOUNT_GBP"],
        config["BUY_ENABLED"],
        config["LAST_BUY_DATE"],
        config["DYNAMIC_DCA"],
    )


def _revalidate_trade_intent(symbol, expected_config, today):
    """Fail closed if the repository-wide rules changed before order submission."""
    live_map = fetch_live_target_map()
    live_state = fetch_live_execution_state()
    live_config = get_config_for_symbol(symbol, live_map, live_state)
    if not live_config["BUY_ENABLED"]:
        raise RuntimeError(f"{symbol} was disabled before order submission")
    if live_config["LAST_BUY_DATE"] == today:
        raise RuntimeError(f"{symbol} is already marked as bought on {today}")
    if not is_time_to_trade(live_config["TIME"]):
        raise RuntimeError(f"{symbol} is no longer due for execution")
    if _trade_rule_snapshot(live_config) != _trade_rule_snapshot(expected_config):
        raise RuntimeError(
            f"{symbol} configuration changed during this run; retry with fresh rules"
        )
    return live_map, live_state


def execute_trade(
    symbol,
    amount_gbp,
    map_key=None,
    target_map=None,
    dca_decision=None,
    expected_config=None,
):
    """Execute one GBP Kraken order and record a confirmed fill."""
    amount_gbp = _parse_amount_gbp(amount_gbp)
    if dca_decision is None:
        dca_decision = _build_dca_decision(
            amount_gbp,
            1.0,
            None,
            "Full buy (x1): Dynamic DCA decision was not available.",
        )

    key = _validate_config_key(map_key or symbol)
    today = datetime.now(SELECTED_TZ).strftime("%Y-%m-%d")
    intent = None
    intent_was_existing = False
    try:
        live_state = fetch_live_execution_state()
        intent = _pending_order_for_symbol(live_state, key)
        if intent is not None:
            intent_was_existing = True
            amount_gbp = _parse_amount_gbp(intent["amount_gbp"])
            print(
                f"Reconciling durable Kraken order intent for {key} "
                f"({intent['client_order_id']})."
            )
        else:
            if expected_config is None:
                raise RuntimeError(
                    f"No expected live configuration was supplied for new order {key}"
                )
            _revalidate_trade_intent(symbol, expected_config, today)
            client_order_id = build_client_order_id(symbol, today)
            intent, intent_was_existing = prepare_order_intent(
                key, client_order_id, today, amount_gbp
            )
            if not intent_was_existing:
                try:
                    # The durable intent now exists. Re-check rules again so an
                    # intervening disable or amount edit clears safely before Kraken.
                    _revalidate_trade_intent(symbol, expected_config, today)
                except Exception as validation_error:
                    raise KrakenPreSubmissionError(
                        "Live rules changed after the durable intent was saved: "
                        f"{validation_error}"
                    ) from validation_error

        client_order_id = intent["client_order_id"]
        amount_gbp = _parse_amount_gbp(intent["amount_gbp"])
        _gha_mask(str(amount_gbp))
        if amount_gbp.is_integer():
            _gha_mask(str(int(amount_gbp)))
        print(f"Executing Kraken DCA buy for {symbol} ({amount_gbp:.2f} GBP).")

        def final_rule_check():
            if intent_was_existing:
                return
            _revalidate_trade_intent(symbol, expected_config, today)

        order_data = place_market_buy(
            symbol,
            amount_gbp,
            client_order_id=client_order_id,
            reconcile_only=intent_was_existing,
            pre_submit_check=final_rule_check,
        )
    except KrakenOrderStateUnknown as error:
        message = (
            f"CRITICAL: Kraken order state is unknown for {key}; the durable "
            f"intent remains locked for reconciliation. {error}"
        )
        print(message)
        send_discord_alert(message, is_error=True)
        return False
    except (KrakenOrderNoFill, KrakenPreSubmissionError) as error:
        if intent is not None:
            try:
                clear_order_intent(key, intent["client_order_id"])
            except Exception as clear_error:
                error = RuntimeError(
                    f"{error}; durable intent cleanup also failed: {clear_error}"
                )
        message = f"DCA failed ({symbol}): {error}"
        print(message)
        send_discord_alert(message, is_error=True)
        return False
    except Exception as error:
        if intent is not None:
            message = (
                f"CRITICAL: Unexpected Kraken failure for {key}; the durable "
                f"intent remains locked for reconciliation. {error}"
            )
        else:
            message = f"DCA failed before an order intent was saved ({symbol}): {error}"
        print(message)
        send_discord_alert(message, is_error=True)
        return False

    order_id = order_data["order_id"]
    exchange_pair = order_data["pair"]
    cost_gbp = float(order_data["cost_gbp"])
    fee_gbp = float(order_data["fee_gbp"])
    gbp_fee_debit = float(order_data["gbp_fee_debit"])
    fee_details = order_data["fee_details"]
    spent_gbp = float(order_data["spent_gbp"])
    received_amount = float(order_data["received"])
    gbp_price_per_unit = float(order_data["market_gbp_price_per_unit"])
    effective_gbp_price_per_unit = float(
        order_data["effective_gbp_price_per_unit"]
    )
    execution_timestamp = int(order_data["timestamp"])
    base_symbol = exchange_pair.split("/", maxsplit=1)[0]

    _gha_mask(str(order_id))
    _gha_mask(f"{cost_gbp:.2f}")
    _gha_mask(f"{fee_gbp:.2f}")
    _gha_mask(f"{gbp_fee_debit:.2f}")
    _gha_mask(f"{spent_gbp:.2f}")
    _gha_mask(f"{received_amount:.8f}")
    _gha_mask(f"{gbp_price_per_unit:.2f}")

    # Completion and pending-intent removal are one write in the trader-only
    # execution-state variable. If it fails, the durable intent remains locked.
    try:
        complete_order_intent(key, client_order_id, intent["trade_date"])
    except Exception as error:
        message = (
            f"CRITICAL: Kraken filled {order_id}, but execution state could not be "
            f"completed for {key}. The durable intent remains locked: {error}"
        )
        print(message)
        send_discord_alert(message, is_error=True)
        return False

    trade_data = {
        "ts": execution_timestamp,
        "amount_crypto": received_amount,
        "amount_gbp": spent_gbp,
        "cost_gbp": cost_gbp,
        "fee_gbp": fee_gbp,
        "gbp_fee_debit": gbp_fee_debit,
        "fee_details": fee_details,
        "order_id": order_id,
        "gbp_price_per_unit": gbp_price_per_unit,
        "effective_gbp_price_per_unit": effective_gbp_price_per_unit,
        "exchange_pair": exchange_pair,
    }

    ghostfolio_saved = False
    try:
        from portfolio_logger import log_to_ghostfolio

        account_id = get_ghostfolio_account_id(base_symbol)
        if account_id:
            ghostfolio_saved = bool(
                log_to_ghostfolio(
                    trade_data,
                    base_symbol,
                    account_id,
                    exchange_pair=exchange_pair,
                )
            )
        else:
            print(f"No Ghostfolio account configured for {base_symbol}.")
    except Exception as error:
        print(f"Ghostfolio logging error: {error}")

    try:
        update_gist_log(
            trade_data,
            symbol=base_symbol,
            saved_to_ghostfolio=ghostfolio_saved,
        )
    except Exception as error:
        print(f"Gist logging error: {error}")

    execution_time = datetime.fromtimestamp(
        execution_timestamp, tz=SELECTED_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")
    message = (
        "DCA buy executed and confirmed.\n"
        f"**Pair:** {exchange_pair}\n"
        f"**Order cost:** £{cost_gbp:,.2f}\n"
        f"**Kraken fee (GBP equivalent):** £{fee_gbp:,.2f}\n"
        f"**Fee charged from GBP:** £{gbp_fee_debit:,.2f}\n"
        f"**Total GBP debit:** £{spent_gbp:,.2f}\n"
        f"**Received:** {received_amount:.8f} {base_symbol}\n"
        f"**Market rate:** £{gbp_price_per_unit:,.2f}\n"
        f"**Effective rate:** £{effective_gbp_price_per_unit:,.2f}\n"
        f"**Asset ROI:** {format_asset_roi(dca_decision.get('roi_percent'))}\n"
        f"**DCA Decision:** {dca_decision.get('reason')}\n"
        f"**Portfolio:** {'Saved' if ghostfolio_saved else 'Not saved'}\n"
        f"**Time:** {execution_time}\n"
        f"**Order ID:** {order_id}"
    )
    send_discord_alert(message, is_error=False)
    return True


def main():
    print("--- Starting Kraken GBP DCA logic ---")
    try:
        target_map = json.loads(DCA_TARGET_MAP_JSON)
        if not isinstance(target_map, dict) or not target_map:
            raise ValueError("DCA_TARGET_MAP must be a non-empty JSON object")
        execution_state = json.loads(DCA_EXECUTION_STATE_JSON or "{}")
        _validate_execution_state(execution_state)
    except (json.JSONDecodeError, ValueError) as error:
        message = f"Invalid DCA configuration or execution state: {error}"
        print(message)
        send_discord_alert(message, is_error=True)
        return False

    print(f"Target map keys: {list(target_map.keys())}")
    all_succeeded = True
    pending_keys = [
        key
        for key, entry in execution_state.items()
        if isinstance(entry, dict) and entry.get(PENDING_ORDER_FIELD) is not None
    ]
    symbols_to_process = list(dict.fromkeys([*target_map, *pending_keys]))
    for symbol in symbols_to_process:
        pending = _pending_order_for_symbol(execution_state, symbol)
        if pending is not None:
            recovery_decision = _build_dca_decision(
                float(pending["amount_gbp"]),
                1.0,
                None,
                "Recovery: reconcile the durable Kraken order intent only.",
            )
            try:
                succeeded = execute_trade(
                    symbol,
                    pending["amount_gbp"],
                    map_key=symbol,
                    target_map=target_map,
                    dca_decision=recovery_decision,
                    expected_config=None,
                )
            except Exception as error:
                message = f"DCA recovery failed ({symbol}): {error}"
                print(message)
                send_discord_alert(message, is_error=True)
                succeeded = False
            if not succeeded:
                all_succeeded = False
            continue

        try:
            config = get_config_for_symbol(symbol, target_map, execution_state)
        except ValueError as error:
            message = f"Rejected DCA configuration: {error}"
            print(message)
            send_discord_alert(message, is_error=True)
            all_succeeded = False
            continue

        if not config["BUY_ENABLED"]:
            print(f"{symbol} is disabled; skipping.")
            continue
        if not is_time_to_trade(config["TIME"]):
            print(f"Not time yet for {symbol} (target: {config['TIME']}).")
            continue

        today = datetime.now(SELECTED_TZ).strftime("%Y-%m-%d")
        if config["LAST_BUY_DATE"] == today:
            print(f"Already bought {symbol} today ({today}); skipping.")
            continue

        decision = determine_dynamic_dca_decision(
            symbol, config["AMOUNT_GBP"], config["DYNAMIC_DCA"]
        )
        print(decision["reason"])
        try:
            succeeded = execute_trade(
                symbol,
                decision["amount_gbp"],
                map_key=config["KEY"],
                target_map=target_map,
                dca_decision=decision,
                expected_config=config,
            )
        except Exception as error:
            message = f"DCA post-fill handling failed ({symbol}): {error}"
            print(message)
            send_discord_alert(message, is_error=True)
            succeeded = False
        if not succeeded:
            all_succeeded = False

    return all_succeeded


if __name__ == "__main__":
    if not main():
        raise SystemExit(1)

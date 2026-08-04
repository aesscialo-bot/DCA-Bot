import os
import time
import json
import math
import requests
from datetime import datetime, timedelta
from gist_logger import update_gist_log
from bitkub_client import bitkub_request, get_thb_usd_rate

# --- Configuration ---
# Timezone Configuration
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
from zoneinfo import ZoneInfo
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)

# Default settings (fallback)
DEFAULT_DCA_AMOUNT = 50.0
DEFAULT_TARGET_TIME = os.environ.get("DCA_TARGET_TIME", "07:00")
DYNAMIC_DCA_DEFAULT_THRESHOLD_PERCENT = -2.0
DYNAMIC_DCA_DEFAULT_REDUCED_MULTIPLIER = 0.5
MINIMUM_DCA_AMOUNT_THB = 10.0

# Target Map (JSON String)
# Format: {"BTC_THB": {"TIME": "07:00", "AMOUNT": 800, "BUY_ENABLED": true, "LAST_BUY_DATE": ""}}
DCA_TARGET_MAP_JSON = os.environ.get("DCA_TARGET_MAP", "{}")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TRADING_EXCHANGE = os.environ.get("TRADING_EXCHANGE", "bitkub").lower()


def _gha_mask(value: str) -> None:
    """Emit a GitHub Actions masking command so the value is redacted in run logs."""
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def send_discord_alert(message, is_error=False):
    if not DISCORD_WEBHOOK_URL:
        # print(f"[Discord Mock] {message}")
        return

    color = 16711680 if is_error else 65280 # Red or Green
    payload = {
        "embeds": [{
            "title": "Crypto DCA Execution",
            "description": message,
            "color": color,
            "timestamp": datetime.now(SELECTED_TZ).isoformat()
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send Discord: {e}")

def get_config_for_symbol(symbol_thb, target_map):
    """
    Resolves the configuration for a given symbol.
    Returns a dict: {"TIME": "HH:MM", "AMOUNT": float, "BUY_ENABLED": bool}
    """
    config = {
        "TIME": DEFAULT_TARGET_TIME, 
        "AMOUNT": DEFAULT_DCA_AMOUNT, 
        "BUY_ENABLED": True,
        "LAST_BUY_DATE": None,
        "DYNAMIC_DCA": None,
        "KEY": symbol_thb # Store the key used in map for updates later
    }
    
    # keys to check in order: "BTC_THB", "BTC/USDT"
    keys_to_check = [symbol_thb]
    try:
        base = symbol_thb.split('_')[0]
        keys_to_check.append(f"{base}/USDT")
    except Exception:
        pass

    found_entry = None
    target_key = symbol_thb
    
    for key in keys_to_check:
        if key in target_map:
            found_entry = target_map[key]
            target_key = key
            break
            
    config["KEY"] = target_key
            
    if found_entry:
        if isinstance(found_entry, dict):
            # New Format
            config["TIME"] = found_entry.get("TIME", DEFAULT_TARGET_TIME)
            config["AMOUNT"] = float(found_entry.get("AMOUNT", DEFAULT_DCA_AMOUNT))
            config["BUY_ENABLED"] = found_entry.get("BUY_ENABLED", True)
            config["LAST_BUY_DATE"] = found_entry.get("LAST_BUY_DATE", None)
            config["DYNAMIC_DCA"] = found_entry.get("DYNAMIC_DCA")
        else:
            # Old Format (String Time)
            config["TIME"] = str(found_entry)
            
    else:
        print(f"⚠️ No config found for {symbol_thb}. Using defaults.")

    return config


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
            dynamic_dca.get("THRESHOLD_PERCENT", DYNAMIC_DCA_DEFAULT_THRESHOLD_PERCENT)
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
        settings["error"] = "DYNAMIC_DCA.REDUCED_MULTIPLIER must be above 0 and at most 1."
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
        print(f"⚠️ Could not read PORTFOLIO_ACCOUNT_MAP: {error}")
    except Exception as error:
        print(f"⚠️ Could not resolve Ghostfolio account for {symbol}: {error}")

    return None


def _build_dca_decision(amount_thb, multiplier, roi_percent, reason):
    return {
        "amount_thb": amount_thb,
        "multiplier": multiplier,
        "roi_percent": roi_percent,
        "reason": reason,
    }


def determine_dynamic_dca_decision(symbol, configured_amount, dynamic_dca):
    """Choose a full or reduced DCA amount from the asset's Ghostfolio ROI."""
    configured_amount = float(configured_amount)
    settings = get_dynamic_dca_settings(dynamic_dca)

    if settings["error"]:
        return _build_dca_decision(
            configured_amount,
            1.0,
            None,
            f"Full buy (x1): {settings['error']}",
        )

    if not settings["enabled"]:
        return _build_dca_decision(
            configured_amount,
            1.0,
            None,
            "Full buy (x1): Dynamic DCA is disabled.",
        )

    base_symbol = symbol.split("_")[0]
    account_id = get_ghostfolio_account_id(base_symbol)
    if not account_id:
        return _build_dca_decision(
            configured_amount,
            1.0,
            None,
            "Full buy (x1): Ghostfolio ROI is unavailable; using the configured amount.",
        )

    try:
        from portfolio_logger import get_asset_roi_percent

        roi_percent = get_asset_roi_percent(
            base_symbol, account_id, exchange_pair=symbol
        )
    except Exception as error:
        print(f"⚠️ Ghostfolio asset ROI lookup failed for {symbol}: {error}")
        roi_percent = None

    if roi_percent is None:
        return _build_dca_decision(
            configured_amount,
            1.0,
            None,
            "Full buy (x1): Ghostfolio ROI is unavailable; using the configured amount.",
        )

    if roi_percent >= settings["threshold_percent"]:
        reduced_amount = round(
            configured_amount * settings["reduced_multiplier"], 2
        )
        if reduced_amount >= MINIMUM_DCA_AMOUNT_THB:
            return _build_dca_decision(
                reduced_amount,
                settings["reduced_multiplier"],
                roi_percent,
                (
                    f"Half buy (x{settings['reduced_multiplier']:g}): asset ROI "
                    f"{roi_percent:+.2f}% is at or above "
                    f"{settings['threshold_percent']:.2f}%."
                ),
            )

        return _build_dca_decision(
            configured_amount,
            1.0,
            roi_percent,
            (
                "Full buy (x1): the reduced amount would be below the "
                f"{MINIMUM_DCA_AMOUNT_THB:.0f} THB minimum."
            ),
        )

    return _build_dca_decision(
        configured_amount,
        1.0,
        roi_percent,
        (
            f"Full buy (x1): asset ROI {roi_percent:+.2f}% is below "
            f"{settings['threshold_percent']:.2f}%."
        ),
    )


def format_asset_roi(roi_percent):
    """Format an asset ROI for a Discord trade notification."""
    if roi_percent is None:
        return "Unavailable"
    return f"{roi_percent:+.2f}%"

def is_time_to_trade(target_time_str):
    """
    Checks if current BKK time matches the target time (HH:MM) within a small window.
    Assumes script runs frequently (e.g. every 15-30 mins).
    We check if current time is within [target, target + 15m).
    """
    now = datetime.now(SELECTED_TZ)
    
    # Parse target
    try:
        t_hour, t_minute = map(int, target_time_str.split(':'))
        target_dt = now.replace(hour=t_hour, minute=t_minute, second=0, microsecond=0)
    except (ValueError, AttributeError) as e:
        print(f"❌ Invalid target time format: {target_time_str} ({e})")
        return False
    
    # If target is tomorrow (e.g. now=23:50, target=00:10), this naive compare fails.
    # But usually we run daily cycle. If target is 00:10 and now is 23:50, diff is huge.
    # If target is 23:50 and now is 00:05 (next day), diff is negative.
    # Simple fix: we only care if NOW is "just after" TARGET.
    
    diff = (now - target_dt).total_seconds()
    
    # Handle day wrap for "just after midnight" if target was late night?
    # No, typically cron runs same day. 
    # If target=23:55 and now=00:05, diff is negative huge?
    # Wait: now(00:05) - target(23:55 today) -> target is in future? No.
    # If now is 00:05, target 23:55 of TODAY is in future. Diff is large negative.
    # So we missed yesterday's window.
    
    # Rules:
    # 1. If within +/- 5 mins of target time -> BUY
    # 2. If target time is in the past (today) -> BUY (Catch-up mechanism)
    #    (The catch-up relies on the "Not bought today" check in main loop)
    
    abs_diff = abs(diff)
    
    # Rule 1: Window check (+/- 5 mins = 300s)
    if abs_diff <= 300:
        print(f"✅ Within window (+/- 5m). Diff={diff:.0f}s")
        return True
        
    # Rule 2: Late check (Target passed today)
    # If diff is positive (Now > Target)
    if diff > 0:
        print(f"✅ Target time passed today. Diff={diff:.0f}s. Catch-up mode.")
        return True
        
    return False

def save_last_buy_date(target_map, symbol_key, date_str):
    """
    Saves LAST_BUY_DATE to GitHub repository variable with retry logic.
    CRITICAL: This is the primary safeguard against double-buys.
    If this fails, we raise an exception to fail the workflow loudly.
    """
    print(f"💾 Saving LAST_BUY_DATE for {symbol_key} as {date_str}...")
    
    # Update local object
    if symbol_key not in target_map:
        target_map[symbol_key] = {}
        
    if not isinstance(target_map[symbol_key], dict):
        # Convert simple "07:00" to dict object to support LAST_BUY_DATE
        target_map[symbol_key] = {
            "TIME": str(target_map[symbol_key]),
            "AMOUNT": DEFAULT_DCA_AMOUNT,
            "BUY_ENABLED": True
        }
        
    target_map[symbol_key]["LAST_BUY_DATE"] = date_str
    
    # Serialize
    new_json = json.dumps(target_map)
    
    # Push to GitHub with retry logic
    token = os.environ.get("GIST_TOKEN") 
    if not token:
        err_msg = "🚨 CRITICAL: No GIST_TOKEN found. Cannot update LAST_BUY_DATE. DOUBLE-BUY RISK!"
        print(err_msg)
        send_discord_alert(err_msg, is_error=True)
        raise RuntimeError(err_msg)

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        err_msg = "🚨 CRITICAL: GITHUB_REPOSITORY env var missing. Cannot update LAST_BUY_DATE. DOUBLE-BUY RISK!"
        print(err_msg)
        send_discord_alert(err_msg, is_error=True)
        raise RuntimeError(err_msg)
    
    url = f"https://api.github.com/repos/{repo}/actions/variables/DCA_TARGET_MAP"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    data = {"name": "DCA_TARGET_MAP", "value": new_json}
    
    # Retry configuration
    max_retries = 3
    retry_delays = [1, 3, 5]  # Exponential-ish backoff: 1s, 3s, 5s
    last_error = None
    
    for attempt in range(max_retries):
        try:
            print(f"   Attempt {attempt + 1}/{max_retries}...")
            r = requests.patch(url, headers=headers, json=data, timeout=15)
            
            if r.status_code == 204:
                print("✅ Successfully updated DCA_TARGET_MAP on GitHub.")
                return  # Success!
            elif r.status_code == 404:
                # Variable doesn't exist, try to create it
                print(f"   Variable not found (404). Attempting to create...")
                create_url = f"https://api.github.com/repos/{repo}/actions/variables"
                r_create = requests.post(create_url, headers=headers, json=data, timeout=15)
                if r_create.status_code == 201:
                    print("✅ Successfully created DCA_TARGET_MAP on GitHub.")
                    return  # Success!
                else:
                    last_error = f"Create failed: {r_create.status_code} {r_create.text}"
            else:
                last_error = f"HTTP {r.status_code}: {r.text}"
                
        except requests.exceptions.Timeout:
            last_error = "Request timed out"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        
        # If not the last attempt, wait and retry
        if attempt < max_retries - 1:
            delay = retry_delays[attempt]
            print(f"   ⚠️ Failed: {last_error}. Retrying in {delay}s...")
            time.sleep(delay)
    
    # All retries exhausted - CRITICAL FAILURE
    err_msg = (
        f"🚨 **CRITICAL: LAST_BUY_DATE UPDATE FAILED** 🚨\n"
        f"Symbol: {symbol_key}\n"
        f"Date: {date_str}\n"
        f"Error: {last_error}\n\n"
        f"⚠️ **DOUBLE-BUY RISK**: The trade was executed but the safeguard was not updated!\n"
        f"**ACTION REQUIRED**: Manually set `LAST_BUY_DATE` to `{date_str}` for `{symbol_key}` in GitHub Variables."
    )
    print(err_msg)
    send_discord_alert(err_msg, is_error=True)
    
    # Raise exception to fail the workflow loudly
    raise RuntimeError(f"Failed to update LAST_BUY_DATE after {max_retries} attempts: {last_error}")

def execute_trade(
    symbol, amount_thb, map_key=None, target_map=None, dca_decision=None
):
    if dca_decision is None:
        dca_decision = _build_dca_decision(
            amount_thb,
            1.0,
            None,
            "Full buy (x1): Dynamic DCA decision was not available.",
        )

    # Mask the configured DCA amount so subsequent log lines are redacted in GitHub Actions
    _gha_mask(str(amount_thb))
    if float(amount_thb) == int(float(amount_thb)):
        _gha_mask(str(int(float(amount_thb))))
    print(f"🚀 Executing DCA Buy for {symbol} ({amount_thb} THB)...")
    
    try:
        if TRADING_EXCHANGE == "kraken":
            from kraken_client import place_market_buy

            order_data = place_market_buy(symbol, amount_thb)
            order_id = order_data["order_id"]
            spent_thb = order_data["spent_thb"]
            received_amt = order_data["received"]
            rate = spent_thb / received_amt
            ts_exec = order_data["timestamp"]
            exchange_pair = order_data["pair"]
            quote_line = (
                f"💷 **Spent ({order_data['quote_currency']}):** "
                f"{order_data['spent_quote']:,.2f}\n"
            )
        elif TRADING_EXCHANGE == "bitkub":
            order_payload = {
                "sym": symbol,
                "amt": amount_thb,
                "rat": 0,
                "typ": "market",
            }
            result = bitkub_request(
                "POST", "/api/v3/market/place-bid", order_payload
            )
            if result.get("error") != 0:
                raise Exception(f"API Error Code: {result.get('error')}")

            order_id = result.get("result", {}).get("id")
            print(f"   Placed Order ID: {order_id}. Waiting for match...")
            time.sleep(5)
            order_data = bitkub_request(
                "GET",
                "/api/v3/market/order-info",
                params={"sym": symbol, "id": order_id, "sd": "buy"},
            ).get("result", {})
            spent_thb = float(order_data.get("filled", 0))
            if spent_thb == 0:
                spent_thb = float(order_data.get("total", 0))
            history = order_data.get("history", [])
            received_amt = sum(
                float(t["amount"]) / float(t["rate"])
                for t in history
                if float(t.get("rate", 0)) > 0
            )
            rate = spent_thb / received_amt if received_amt > 0 else 0
            ts_exec = int(order_data.get("ts", time.time()))
            exchange_pair = symbol
            quote_line = ""
        else:
            raise ValueError(
                "TRADING_EXCHANGE must be either 'bitkub' or 'kraken'"
            )

        _gha_mask(str(order_id))
        # Mask all sensitive trade values so they are redacted in GitHub Actions run logs
        _gha_mask(f"{spent_thb:.2f}")
        _gha_mask(f"{received_amt:.8f}")
        _gha_mask(f"{rate:.2f}")
        dt_str = datetime.fromtimestamp(ts_exec, tz=SELECTED_TZ).strftime('%Y-%m-%d %H:%M:%S')

        # 4. Calculate USD value
        base_sym = symbol.split('_')[0]
        fx_rate = get_thb_usd_rate()
        
        if fx_rate == 0:
            # FX rate fetch failed - send error notification
            fx_error_msg = (
                f"⚠️ **FX Rate Fetch Failed**\n"
                f"Trade executed successfully but USD conversion unavailable.\n"
                f"All currency exchange API sources failed."
            )
            send_discord_alert(fx_error_msg, is_error=True)
        
        usd_spent = spent_thb * fx_rate if fx_rate > 0 else 0
        usd_price_per_unit = (usd_spent / received_amt) if received_amt > 0 else 0
        _gha_mask(f"{usd_spent:.2f}")
        _gha_mask(f"{usd_price_per_unit:.4f}")

        # 5. Log to Ghostfolio
        ghostfolio_saved = False
        try:
            from portfolio_logger import log_to_ghostfolio
            
            account_id = get_ghostfolio_account_id(base_sym)
            
            if account_id:
                ghostfolio_data = {
                    "ts": ts_exec,
                    "amount_crypto": received_amt,
                    "amount_thb": spent_thb,
                    "amount_usd": usd_spent,
                    "symbol": base_sym,
                    "order_id": order_id,
                    "usd_price_per_unit": usd_price_per_unit
                }
                
                ghostfolio_saved = log_to_ghostfolio(
                    ghostfolio_data,
                    base_sym,
                    account_id,
                    exchange_pair=exchange_pair,
                )
                
                if ghostfolio_saved:
                    print(f"✅ Logged to Ghostfolio account {account_id}")
                else:
                    print(f"⚠️ Failed to log to Ghostfolio")
            else:
                print(f"⚠️ No Ghostfolio account configured for {base_sym}")
                
        except Exception as e:
            print(f"⚠️ Ghostfolio logging error: {e}")

        # 6. Log to Gist
        update_gist_log({
            "ts": ts_exec,
            "amount_thb": spent_thb,
            "price": rate,
            "amount_btc": received_amt, # Generic field name, but holds crypto amount
            "usd_rate": 0, 
            "order_id": order_id
        }, symbol=base_sym, saved_to_ghostfolio=ghostfolio_saved)

        # 7. Notify Discord
        msg = (
            f"✅ **DCA Buy Executed!**\n"
            f"🔹 **Pair:** {exchange_pair}\n"
            f"💰 **Spent:** ฿{spent_thb:,.2f}\n"
            f"{quote_line}"
            f"💵 **Spent (USD):** ${usd_spent:,.2f}\n"
            f"📥 **Received:** {received_amt:.8f} {base_sym}\n"
            f"🏷️ **Rate:** ฿{rate:,.2f}\n"
            f"🏷️ **Rate (USD):** ${usd_price_per_unit:,.4f}\n"
            f"📊 **Asset ROI:** {format_asset_roi(dca_decision.get('roi_percent'))}\n"
            f"⚖️ **DCA Decision:** {dca_decision.get('reason')}\n"
            f"💾 **Portfolio:** {'✅ Saved' if ghostfolio_saved else '❌ Not saved'}\n"
            f"🕒 **Time:** {dt_str}\n"
            f"🆔 **Order ID:** {order_id}"
        )
        send_discord_alert(msg, is_error=False)

    except Exception as e:
        err = f"❌ **DCA Failed ({symbol})**: {str(e)}"
        print(err)
        send_discord_alert(err, is_error=True)
        # Fall through — LAST_BUY_DATE is still saved below to prevent
        # the bot from hammering a broken API/insufficient funds every 15 min.

    # 8. Update LAST_BUY_DATE in DCA_TARGET_MAP (always — success or failure)
    # CRITICAL: Outside try/except so RuntimeError from save_last_buy_date propagates
    # and the workflow fails loudly instead of silently swallowing the double-buy risk.
    if map_key and target_map:
        today_str = datetime.now(SELECTED_TZ).strftime("%Y-%m-%d")
        print(f"🔄 Updating LAST_BUY_DATE for {map_key} to {today_str}...")
        save_last_buy_date(target_map, map_key, today_str)

def main():
    print(f"--- Starting DCA Logic ---")
    
    # Parse Target Map
    try:
        target_map = json.loads(DCA_TARGET_MAP_JSON)
    except Exception:
        print("⚠️ Failed to parse DCA_TARGET_MAP JSON. Using empty map.")
        target_map = {}

    print(f"Target Map Keys: {list(target_map.keys())}")

    # Determine symbols to process
    symbols_to_process = []
    for k in target_map.keys():
        if isinstance(target_map[k], dict):
             # Check if explicitly disabled, if enabled or missing key -> include
             if target_map[k].get("BUY_ENABLED", True):
                 symbols_to_process.append(k)
             else:
                 print(f"🚫 {k} is DISABLED in config. Skipping.")
        else:
             # Legacy string format -> Assume enabled
            symbols_to_process.append(k)
    
    # Clean list
    symbols_to_process = [s.strip() for s in symbols_to_process if s.strip()]

    print(f"Symbols to Process (Enabled): {symbols_to_process}")

    for symbol in symbols_to_process:
        print(f"\nPROCESSING {symbol}...")
        
        config = get_config_for_symbol(symbol, target_map)
        
        # BUY_ENABLED check is redundant if we filtered above, but good for safety
        if not config["BUY_ENABLED"]:
            print(f"⛔ Trade Disabled for {symbol}. Skipping.")
            continue
            
        target_time = config["TIME"]
        configured_amount = config["AMOUNT"]
        
        if is_time_to_trade(target_time):
            # Check LAST_BUY_DATE
            today_str = datetime.now(SELECTED_TZ).strftime("%Y-%m-%d")
            last_buy = config.get("LAST_BUY_DATE")
            
            if last_buy == today_str:
                print(f"🛑 Already bought {symbol} today ({today_str}). Skipping.")
            else:
                dca_decision = determine_dynamic_dca_decision(
                    symbol, configured_amount, config["DYNAMIC_DCA"]
                )
                print(
                    "✅ Time match & Not bought today! "
                    f"{dca_decision['reason']}"
                )
                execute_trade(
                    symbol,
                    dca_decision["amount_thb"],
                    map_key=config["KEY"],
                    target_map=target_map,
                    dca_decision=dca_decision,
                )
        else:
            print(f"⏳ Not time yet (Target: {target_time}). Skipping.")

if __name__ == "__main__":
    main()

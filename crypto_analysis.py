import ccxt
import pandas as pd
import requests
import os
import re
import json
import time
from google import genai
from datetime import datetime, timedelta, timezone

# --- Config ---
EXCHANGE_ID = os.environ.get("EXCHANGE_ID", "kraken")
# Support comma-separated list OR JSON array
SYMBOLS_ENV = os.environ.get("SYMBOL", "")

def _parse_symbols(symbols_env: str, dca_map_env: str) -> list:
    """Resolve the list of symbols to analyze.

    Priority:
      1. Explicit SYMBOL env var (comma-separated or JSON array).
      2. Derive from canonical DCA_TARGET_MAP keys (BTC_GBP -> BTC/GBP).

    When neither source is valid, fail closed instead of analyzing an asset that
    was never configured.
    """
    # 1. Explicit SYMBOL env var
    if symbols_env.strip():
        try:
            parsed = json.loads(symbols_env)
            if isinstance(parsed, list):
                result = [str(s) for s in parsed]
            else:
                result = [str(parsed)]
        except (json.JSONDecodeError, ValueError):
            result = [s.strip() for s in symbols_env.split(",") if s.strip()]
        normalized = []
        for symbol in result:
            candidate = symbol.upper().replace("_", "/")
            if "/" not in candidate:
                candidate = f"{candidate}/GBP"
            if not re.fullmatch(r"[A-Z0-9]+/GBP", candidate):
                raise ValueError(f"Only GBP analysis pairs are supported: {symbol}")
            normalized.append(candidate)
        result = normalized
        print(f"📋 Symbols from SYMBOL env var: {result}")
        return result

    # 2. Derive from DCA_TARGET_MAP
    try:
        dca_map = json.loads(dca_map_env) if dca_map_env else {}
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("DCA_TARGET_MAP is not valid JSON") from error

    if not isinstance(dca_map, dict):
        raise ValueError("DCA_TARGET_MAP must be a JSON object")

    if dca_map:
        symbols = []
        for key in dca_map:
            if isinstance(key, str) and re.fullmatch(r"[A-Z0-9]+_GBP", key):
                symbols.append(f"{key[:-4]}/GBP")
        if symbols:
            print(f"📋 Symbols derived from DCA_TARGET_MAP: {symbols}")
            return symbols
        raise ValueError("DCA_TARGET_MAP contains no canonical COIN_GBP keys")

    raise ValueError(
        "No Kraken GBP analysis symbols were provided and DCA_TARGET_MAP is empty"
    )


TIMEFRAME = "15m"
LOCAL_TZ = os.environ.get("TIMEZONE", "Asia/Bangkok")
# Kraken Spot REST returns at most 720 candles. At 15 minutes that is 7.5 days,
# so these periods deliberately stay within the exchange's documented window.
PERIODS = [3, 5, 7]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DCA_TARGET_MAP_ENV = os.environ.get("DCA_TARGET_MAP", "{}")
SHORT_REPORT = os.environ.get("SHORT_REPORT", "true").lower() == "true"
try:
    EXISTING_MAP = json.loads(DCA_TARGET_MAP_ENV)
except Exception:
    EXISTING_MAP = {}


# --- Helpers ---

def _harmonic_mean(series):
    """Harmonic mean of a numeric series — used for DCA price averaging."""
    return len(series) / (1 / series).sum()


def get_analysis_exchange(exchange_id=EXCHANGE_ID):
    """Return the one supported public market-data client."""
    if str(exchange_id).strip().lower() != "kraken":
        raise ValueError("Crypto analysis supports Kraken GBP markets only")
    return ccxt.kraken({"enableRateLimit": True})


# --- Fetch helper ---
def fetch_ohlcv_last_n_days(exchange, symbol, timeframe, days):
    # Add a small buffer to ensure we cover the range fully
    since = int((datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp() * 1000)
    all_rows = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1500)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        since = last_ts + 1
        # stop if we're mostly caught up
        if last_ts >= int(datetime.now(timezone.utc).timestamp() * 1000) - 3600 * 1000:
            break
    return all_rows

def analyze_period(df, days, local_tz):
    # Filter for the specific lookback period based on the max timestamp in df
    # (assuming df end is "now", so we subtract days from the end time)
    end_time = df["ts"].max()
    start_time = end_time - pd.Timedelta(days=days)
    period_df = df[df["ts"] >= start_time].copy()
    
    # Analysis 1: Most common daily-low time
    idx = period_df.groupby("local_date")["low"].idxmin()
    daily_lows = period_df.loc[idx, ["local_date", "local_time", "low"]]

    most_common = (
        daily_lows["local_time"]
        .value_counts()
        .sort_index()
        .rename("days_won")
        .reset_index()
        .rename(columns={"index": "time"})
    )
    most_common["share"] = most_common["days_won"] / most_common["days_won"].sum()
    top_common = most_common.sort_values("days_won", ascending=False).head(5)

    # Analysis 2: Lowest average Low by time-of-day
    # (Existing arithmetic mean of 'low')
    avg_low_by_time = (
        period_df.groupby("local_time")["low"]
        .mean()
        .reset_index()
        .rename(columns={"local_time": "time", "low": "avg_low"})
        .sort_values("avg_low", ascending=True)
    )
    # Fix: Define top_avg here so it can be returned
    top_avg = avg_low_by_time.head(5)

    # Analysis 3: Advanced DCA Metrics
    # A. Calculate Daily Average (Mean of O,H,L,C for the day)
    period_df["candle_avg"] = period_df[["open", "high", "low", "close"]].mean(axis=1)
    
    # map daily average back to each row
    daily_means = period_df.groupby("local_date")["candle_avg"].transform("mean")
    period_df["diff_from_daily_avg"] = (period_df["close"] - daily_means) / daily_means * 100

    # B. Calculate "Proximity to Daily Low" (The "Regret" Metric)
    # How much did we overpay vs the absolute bottom of that specific day?
    daily_min_low = period_df.groupby("local_date")["low"].transform("min")
    period_df["miss_pct"] = (period_df["close"] - daily_min_low) / daily_min_low * 100
    
    # NEW: Win Rate (Consistency Metric)
    # "Win" = Price is within 0.5% (50bps) of the absolute daily low
    period_df["is_snipe"] = period_df["miss_pct"] < 0.5

    # C. Group by time
    dca_group = period_df.groupby("local_time")

    dca_stats = dca_group.agg(
        dca_price=("close", _harmonic_mean),
        median_miss=("miss_pct", "median"),
        win_rate=("is_snipe", "mean")
    ).reset_index().rename(columns={"local_time": "time"})
    
    dca_stats["win_rate"] = dca_stats["win_rate"] * 100
    
    # Sort by lowest "miss" from the daily bottom (Median is more robust against crash wicks)
    top_dca = dca_stats.sort_values("median_miss", ascending=True).head(5)

    return top_common, top_avg, top_dca, period_df["ts"].min(), period_df["ts"].max()

def get_ai_summary(full_report, current_symbol):
    if not GEMINI_API_KEY:
        return "No GEMINI_API_KEY found. Skipping AI analysis.", None, None

    try:
        prompt = f"""
        You are a crypto DCA timing analyst. Your job is to choose ONE daily buy time (HH:MM) for {current_symbol} from the report below.

        METRICS (from the report):
        - median_miss: Median % overpayment vs the day’s absolute low. LOWER is better. This is the PRIMARY objective.
        - win_rate: % of days where buying at that time was within 0.5% of the absolute low. HIGHER is better. This is SECONDARY (stability).

        IMPORTANT:
        - median_miss is robust; win_rate depends on the 0.5% threshold and can be noisy.
        - Do NOT invent numbers. Only use values in the report.
        - Only choose times that appear in the report’s “Best DCA Time” tables.

        TASK:
        1) Pick ONE RECOMMENDED_TIME using the decision rules below.
        2) Give a short reason (max 3 sentences) mentioning which timeframe(s) drove the decision.

        DECISION RULES (follow in order):
        A) Recency Shift Check (3-day override)
        - Identify the best 3-day candidate by PRIMARY objective (lowest median_miss).
        - Only override longer timeframes with the 3-day candidate if BOTH are true:
        1) Its win_rate is at least 10 percentage points higher than the best 7-day candidate, AND
        2) Its median_miss is no more than 0.20 percentage points worse than the best 7-day candidate.

        B) Base Selection (5/7-day weighted, median_miss-first)
        - Compare the best 5-day and 7-day candidates.
        - Prefer the 7-day candidate unless the 5-day median_miss is better by at least 0.15 percentage points.

        C) Consistency Bonus (only as tie-break)
        - If multiple candidates are within 0.10 percentage points median_miss of the current choice in the chosen base timeframe:
        - Pick the one that appears in the Top 5 across the most timeframes (3/5/7).
        - If still tied, pick the higher win_rate in the 7-day table.
        - If still tied, pick the earlier time (HH:MM).

        OUTPUT FORMAT (exactly, no extra text):
        RECOMMENDED_TIME: HH:MM
        REASON: <max 5 sentences, cite which rules/timeframes caused the decision>

        Report:
        {full_report}
        """
        
        # Use only the Flash family selected for this deployment. This task needs
        # structured extraction, not a higher-cost reasoning-first model.
        candidates = [
            'gemini-2.5-flash-lite',   # Optimized for speed/volume
            'gemini-2.5-flash',        # More capable Flash fallback
        ]

        result_text = None
        last_error = None

        for model_name in candidates:
            try:
                print(f"Trying AI model: {model_name}...")
                with genai.Client(api_key=GEMINI_API_KEY) as ai_client:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                result_text = response.text.strip()
                break # Stop after the first successful model
            except Exception as e:
                last_error = e
                # creating a short error string to print
                err_str = str(e).split('\n')[0] 
                print(f"  -> Failed: {err_str}...")
                # Wait before trying the next candidate to avoid rate-locking
                if model_name != candidates[-1]:
                    print("  -> Waiting 1mn before next model attempt...")
                    time.sleep(60)
        
        if result_text:
            # Try to extract the time
            match = re.search(r"RECOMMENDED_TIME:\s*(\d{2}:\d{2})", result_text)
            extracted_time = match.group(1) if match else None
            return result_text, extracted_time, model_name
        else:
            return f"AI Analysis failed after trying all candidates. Last error: {last_error}", None, None
    except Exception as e:
        return f"AI Analysis failed: {e}", None, None

def send_to_discord(report_content, color=3447003):
    """Send a report to Discord via webhook.

    Args:
        report_content: Text to send.
        color: Embed sidebar color (default: blue 3447003).
    """
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL found. Skipping Discord notification.")
        return

    # Discord embeds have a 4096 char limit for description
    # Split into chunks of ~3900 chars to stay safe
    chunks = [report_content[i:i+3900] for i in range(0, len(report_content), 3900)]
    
    for i, chunk in enumerate(chunks):
        payload = {
            "embeds": [{
                "description": chunk,
                "color": color
            }]
        }
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            r.raise_for_status()
            print(f"Sent chunk {i+1}/{len(chunks)} to Discord")
        except Exception as e:
            print(f"Failed to send to Discord: {e}")

def main():
    symbols = _parse_symbols(SYMBOLS_ENV, DCA_TARGET_MAP_ENV)
    exchange = get_analysis_exchange()

    map_was_updated = False  # Track whether any TIME value was actually changed

    for symbol in symbols:
        print(f"\nExample: PROCESSING {symbol}...")
        report_lines = []
        summary_lines = []  # For short report
        
        def log(s, summary_only=False):
            """Log to console and report. If summary_only=True, only add to summary."""
            if summary_only:
                # Summary content - always printed, added to summary lines
                print(s)
                summary_lines.append(s)
                if not SHORT_REPORT:
                    report_lines.append(s)  # In full mode, summary is part of report
            else:
                # Detailed content - always added to report (for AI), conditionally printed
                if not SHORT_REPORT:
                    print(s)
                report_lines.append(s)  # Always build full report for AI analysis

        print(f"Fetching max required data ({max(PERIODS)} days) for {symbol}...")
        if not SHORT_REPORT:
            log(f"Fetching max required data ({max(PERIODS)} days) for {symbol}...")
        
        try:
            # Fetch enough data for the largest period
            rows = fetch_ohlcv_last_n_days(exchange, symbol, TIMEFRAME, max(PERIODS))

            # Process into main DataFrame
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.drop_duplicates(subset=["ts"]).sort_values("ts")

            # Pre-calculate local times
            df["local_ts"] = df["ts"].dt.tz_convert(LOCAL_TZ)
            df["local_date"] = df["local_ts"].dt.date
            df["local_time"] = df["local_ts"].dt.strftime("%H:%M")

            if not SHORT_REPORT:
                log(f"Timezone: {LOCAL_TZ}")

            best_overall_time = None
            
            for days in PERIODS:
                log(f"\n{'='*40}")
                log(f" ANALYSIS FOR LAST {days} DAYS ({symbol})")
                log(f"{'='*40}")
                
                try:
                    top_common, top_avg, top_dca, start, end = analyze_period(df, days, LOCAL_TZ)
                    
                    # Use the longest Kraken-supported period as the final candidate.
                    if days == max(PERIODS) and not top_dca.empty:
                        best_overall_time = top_dca.iloc[0]['time']
                        log(f"🏆 CHAMPION TIME ({days} Days): {best_overall_time}")
                    
                    log(f"Range: {start} -> {end}")
                    
                    log("\n(1) Most frequent DAILY-LOW time:")
                    log(top_common.to_string(index=False))

                    log("\n(2) Best DCA Time (Lowest Median Miss from Daily Low):")
                    # Show price and the average discount relative to daily mean
                    log(top_dca.to_string(index=False))
                    log("* 'median_miss': Median % overpayment vs day's absolute low.")
                    log("* 'win_rate': % of days where the buy was within 0.5% of the absolute low.")
                    
                except Exception as e:
                    log(f"Could not analyze {days} days: {e}")

            # After loop, prepare for AI analysis (always use full detailed report)
            final_time = best_overall_time
            source_method = "Quantitative (7d Kraken GBP Median Miss)"

            if GEMINI_API_KEY:
                log("\n" + "="*40, summary_only=True)
                log("🤖 AI ANALYSIS & RECOMMENDATION", summary_only=True)
                log("="*40, summary_only=True)
                
                # For AI, we need the full detailed report
                detailed_report = "\n".join(report_lines)
                ai_summary, ai_time, used_model = get_ai_summary(detailed_report, symbol)
                
                if used_model:
                    log(f"🧠 Model Used: {used_model}", summary_only=True)
                    
                log(ai_summary, summary_only=True)
                
                if ai_time:
                    log(f"\n✨ AI Recommendation Identified: {ai_time}", summary_only=True)
                    if ai_time != final_time:
                        log(f"🔄 Switching target from {final_time} (Math) to {ai_time} (AI)", summary_only=True)
                        final_time = ai_time
                        source_method = "🤖 AI Recommendation"
                    else:
                        log("✅ AI agrees with Quantitative Analysis.", summary_only=True)
                        source_method = f"🤝 Consensus (AI + Math)"
                else:
                    log("⚠️ Could not extract valid time from AI. Sticking to math-based time.", summary_only=True)

            # Build final report for Discord
            log(f"\n🎯 FINAL DECISION for {symbol}: {final_time}", summary_only=True)
            log(f"ℹ️ SOURCE: {source_method}", summary_only=True)
            
            # Update EXISTING_MAP with the final time BEFORE building the Discord report
            # so the appended DCA_TARGET_MAP snapshot reflects the new recommended times.
            if final_time:
                # Normalize to the canonical Kraken GBP configuration key.
                base = symbol.split('/')[0]
                gbp_key = f"{base}_GBP"

                target_key = gbp_key

                # Update Logic
                if target_key in EXISTING_MAP and isinstance(EXISTING_MAP[target_key], dict):
                    old_time = EXISTING_MAP[target_key].get("TIME")
                    EXISTING_MAP[target_key]["TIME"] = final_time
                    if final_time != old_time:
                        map_was_updated = True
                    log(
                        f"✅ Recommended TIME for existing target '{target_key}': "
                        f"{final_time}. The workflow will merge it if live rules are unchanged."
                    )
                elif target_key in EXISTING_MAP:
                    log(
                        f"⚠️ '{target_key}' is not an object and was not updated.",
                        summary_only=True,
                    )
                else:
                    # Symbol not in DCA_TARGET_MAP (new manual-dispatch analysis).
                    # Analysis is complete and Discord report has been sent below,
                    # but we intentionally do NOT add it to the map — the user must
                    # add it manually via the Discord bot or GitHub Variables UI.
                    log(f"ℹ️ '{target_key}' is not in DCA_TARGET_MAP. Recommended time: {final_time}. Add it manually to start trading.", summary_only=True)

            # Determine what to send to Discord
            if SHORT_REPORT:
                full_report = "\n".join(summary_lines)
            else:
                full_report = "\n".join(report_lines)

            # Send individual report per symbol
            send_to_discord(full_report)


        except Exception as e:
             error_msg = f"CRITICAL FAILURE processing {symbol}: {e}"
             print(error_msg)
             send_to_discord(f"❌ Analysis Failed for {symbol}: {e}")

    # Send final DCA_TARGET_MAP snapshot (reflects all TIME updates from this run)
    if EXISTING_MAP:
        label = (
            "DCA_TARGET_MAP (recommended TIME snapshot)"
            if map_was_updated
            else "DCA_TARGET_MAP"
        )
        lines = [f"**📋 {label}**\n"]
        for symbol, config in EXISTING_MAP.items():
            if isinstance(config, dict):
                enabled = config.get("BUY_ENABLED", False)
                status = "🟢" if enabled else "🔴"
                lines.append(
                    f"{status} **{symbol}** — "
                    f"Time: `{config.get('TIME', '?')}`, "
                    f"Amount: `£{config.get('AMOUNT_GBP', '?')}`"
                )
            else:
                lines.append(f"🟢 **{symbol}** — `{config}`")
        send_to_discord("\n".join(lines))

    # Export the merged map for GitHub Actions
    if EXISTING_MAP:
        # We export the MODIFIED existing map, not just the new results
        # Ensure json format matches what GH expects
        json_map = json.dumps(EXISTING_MAP)
        
        # If running locally without GITHUB_OUTPUT
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"best_time_map={json_map}\n")
        else:
            print(f"DEBUG: (Not in GHA) best_time_map={json_map}")

if __name__ == "__main__":
    main()

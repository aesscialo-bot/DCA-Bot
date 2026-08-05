"""Deterministic Kraken GBP regime and daily execution-time analysis.

Gemini is an optional narrator only.  All spend-affecting outputs (regime,
amount tier, and execution time) are calculated locally from completed Kraken
candles and are persisted as ``DCA_ANALYSIS_STATE``.
"""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

import ccxt
from google import genai
import pandas as pd
import requests

from dca_config import (
    ANALYSIS_STATE_VERSION,
    ERROR_STATUS,
    READY_STATUS,
    TARGET_KEYS,
    TARGET_SYMBOLS,
    default_rules_map,
    empty_analysis_state,
    rules_hash,
    validate_analysis_state,
    validate_rules_map,
)


EXCHANGE_ID = os.environ.get("EXCHANGE_ID", "kraken")
SYMBOLS_ENV = os.environ.get("SYMBOL", "")
LOCAL_TZ = os.environ.get("TIMEZONE", "Asia/Bangkok")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DCA_TARGET_MAP_ENV = os.environ.get("DCA_TARGET_MAP", "{}")
DCA_ANALYSIS_STATE_ENV = os.environ.get("DCA_ANALYSIS_STATE", "")
ANALYSIS_VARIABLE = "DCA_ANALYSIS_STATE"
PERIODS = (3, 5, 7)

DAILY_TIMEFRAME_MS = 24 * 60 * 60 * 1000
WEEKLY_TIMEFRAME_MS = 7 * DAILY_TIMEFRAME_MS
QUARTER_HOUR_MS = 15 * 60 * 1000
MIN_DAILY_CANDLES = 220
MIN_WEEKLY_CANDLES = 20
KRAKEN_OHLCV_LIMIT = 720
INTRADAY_CANDLES_PER_DAY = 96
INTRADAY_HISTORY_CANDLES = 7 * INTRADAY_CANDLES_PER_DAY
MAX_DAILY_DATA_AGE = timedelta(hours=30)
MAX_WEEKLY_DATA_AGE = timedelta(days=7, hours=12)
MAX_INTRADAY_DATA_AGE = timedelta(minutes=45)


class AnalysisError(RuntimeError):
    """A fail-closed market-data or deterministic-analysis error."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalysisError("Analysis timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _target_from_symbol(value: str) -> str:
    candidate = value.strip().strip("\"'").upper().replace("_", "/")
    if "/" not in candidate:
        candidate = f"{candidate}/GBP"
    target = candidate.replace("/", "_")
    if target not in TARGET_KEYS:
        raise ValueError(
            f"Only BTC/GBP, ETH/GBP, SOL/GBP, and ADA/GBP are supported: {value}"
        )
    return target


def _parse_symbols(symbols_env: str, dca_map_env: str) -> list[str]:
    """Return a deterministic list of supported Kraken GBP pair strings."""

    if symbols_env.strip() and symbols_env.strip().lower() != "all":
        try:
            decoded = json.loads(symbols_env)
            values = decoded if isinstance(decoded, list) else [decoded]
        except (json.JSONDecodeError, ValueError):
            values = [item.strip() for item in symbols_env.split(",") if item.strip()]
        targets = []
        for value in values:
            if str(value).strip().lower() == "all":
                return _parse_symbols("", dca_map_env)
            target = _target_from_symbol(str(value))
            if target not in targets:
                targets.append(target)
        if not targets:
            raise ValueError("SYMBOL did not contain a supported Kraken GBP pair")
        return [TARGET_SYMBOLS[target] for target in targets]

    try:
        rules = validate_rules_map(dca_map_env)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return [TARGET_SYMBOLS[target] for target in TARGET_KEYS if target in rules]


def get_analysis_exchange(exchange_id: str = EXCHANGE_ID):
    """Create the one supported public market-data client."""

    if str(exchange_id).strip().lower() != "kraken":
        raise ValueError("Crypto analysis supports Kraken GBP markets only")
    return ccxt.kraken({"enableRateLimit": True})


def fetch_ohlcv_last_n_days(exchange, symbol: str, timeframe: str, days: int) -> list:
    """Fetch Kraken's latest bounded OHLCV page without false pagination.

    Kraken exposes at most 720 recent candles regardless of ``since``.  A
    pagination loop can therefore create the illusion of an older history while
    returning overlapping rows.  Timing analysis needs 672 completed 15-minute
    candles, so one latest-page request is both sufficient and deterministic.
    """

    if timeframe == "15m" and days > 7:
        raise AnalysisError("15-minute timing history is limited to 7 rolling days")
    batch = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=KRAKEN_OHLCV_LIMIT,
    )
    rows: dict[int, list] = {}
    for row in batch or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            rows[int(row[0])] = list(row[:6])
    return [rows[key] for key in sorted(rows)]


def _completed_frame(
    rows: Iterable[Iterable[Any]],
    timeframe_ms: int,
    *,
    now: datetime,
    label: str,
) -> pd.DataFrame:
    """Normalize OHLCV and remove the current, not-yet-completed candle."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise AnalysisError("now must include a timezone")
    parsed = []
    for row in rows:
        values = list(row)
        if len(values) < 6:
            continue
        try:
            timestamp = int(values[0])
            numbers = [float(value) for value in values[1:6]]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(number) for number in numbers):
            continue
        if timestamp + timeframe_ms <= int(now.timestamp() * 1000):
            parsed.append([timestamp, *numbers])
    if not parsed:
        raise AnalysisError(f"{label} has no completed Kraken candles")
    frame = pd.DataFrame(
        parsed, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    frame = frame.drop_duplicates("timestamp_ms", keep="last").sort_values("timestamp_ms")
    frame["ts"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True)
    return frame.reset_index(drop=True)


def _require_fresh_candle(
    frame: pd.DataFrame,
    timeframe_ms: int,
    max_age: timedelta,
    *,
    now: datetime,
    label: str,
) -> None:
    latest_end = frame.iloc[-1]["ts"].to_pydatetime() + timedelta(milliseconds=timeframe_ms)
    age = now.astimezone(timezone.utc) - latest_end
    if age < timedelta(0):
        raise AnalysisError(f"{label} contains a candle from the future")
    if age > max_age:
        raise AnalysisError(f"{label} is stale ({age.total_seconds() / 3600:.1f} hours old)")


def _contiguous_tail(
    frame: pd.DataFrame,
    candle_count: int,
    timeframe_ms: int,
    *,
    label: str,
) -> pd.DataFrame:
    """Return the latest exact-cadence candles or fail on shortages and gaps."""

    if len(frame) < candle_count:
        raise AnalysisError(
            f"{label} is insufficient: {len(frame)} completed candles; need {candle_count}"
        )
    tail = frame.tail(candle_count).copy().reset_index(drop=True)
    timestamps = tail["timestamp_ms"].astype("int64")
    gaps = timestamps.diff().dropna()
    if not bool((gaps == timeframe_ms).all()):
        bad_gap = int(gaps[gaps != timeframe_ms].iloc[0])
        expected_minutes = timeframe_ms // 60_000
        actual_minutes = bad_gap / 60_000
        raise AnalysisError(
            f"{label} has a candle gap: expected exact {expected_minutes}-minute "
            f"cadence, found {actual_minutes:g} minutes"
        )
    return tail


def classify_trend(
    daily_rows: Iterable[Iterable[Any]],
    weekly_rows: Iterable[Iterable[Any]],
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Classify UPTREND/DOWNTREND/SIDEWAYS from completed candles only."""

    reference = now or _utc_now()
    daily = _completed_frame(
        daily_rows, DAILY_TIMEFRAME_MS, now=reference, label="Daily market data"
    )
    weekly = _completed_frame(
        weekly_rows, WEEKLY_TIMEFRAME_MS, now=reference, label="Weekly market data"
    )
    if len(daily) < MIN_DAILY_CANDLES:
        raise AnalysisError(
            f"Daily market data is insufficient: {len(daily)} completed candles; "
            f"need {MIN_DAILY_CANDLES}"
        )
    if len(weekly) < MIN_WEEKLY_CANDLES:
        raise AnalysisError(
            f"Weekly market data is insufficient: {len(weekly)} completed candles; "
            f"need {MIN_WEEKLY_CANDLES}"
        )
    # Indicators and the two-candle confirmation must never bridge a missing
    # Kraken period. Only the latest data that participates in the decision is
    # required, preserving harmless older history while failing closed on a
    # recent daily or weekly gap.
    daily = _contiguous_tail(
        daily,
        MIN_DAILY_CANDLES,
        DAILY_TIMEFRAME_MS,
        label="Daily market data",
    )
    weekly = _contiguous_tail(
        weekly,
        MIN_WEEKLY_CANDLES,
        WEEKLY_TIMEFRAME_MS,
        label="Weekly market data",
    )
    _require_fresh_candle(
        daily,
        DAILY_TIMEFRAME_MS,
        MAX_DAILY_DATA_AGE,
        now=reference,
        label="Daily market data",
    )
    _require_fresh_candle(
        weekly,
        WEEKLY_TIMEFRAME_MS,
        MAX_WEEKLY_DATA_AGE,
        now=reference,
        label="Weekly market data",
    )

    daily["sma150"] = daily["close"].rolling(150, min_periods=150).mean()
    daily["ema20"] = daily["close"].ewm(span=20, adjust=False).mean()
    daily["ema50"] = daily["close"].ewm(span=50, adjust=False).mean()
    weekly["ema20"] = weekly["close"].ewm(span=20, adjust=False).mean()

    current = daily.iloc[-1]
    previous = daily.iloc[-2]
    slope_start = daily.iloc[-21]["sma150"]
    slope_end = current["sma150"]
    if pd.isna(slope_start) or pd.isna(slope_end):
        raise AnalysisError("SMA150 20-day slope cannot be calculated")
    weekly_current = weekly.iloc[-1]

    two_day_above = bool(
        current["close"] > current["sma150"]
        and current["ema20"] > current["ema50"]
        and previous["close"] > previous["sma150"]
        and previous["ema20"] > previous["ema50"]
    )
    two_day_below = bool(
        current["close"] < current["sma150"]
        and current["ema20"] < current["ema50"]
        and previous["close"] < previous["sma150"]
        and previous["ema20"] < previous["ema50"]
    )
    weekly_above = bool(weekly_current["close"] > weekly_current["ema20"])
    weekly_below = bool(weekly_current["close"] < weekly_current["ema20"])
    slope = float(slope_end - slope_start)
    slope_positive = slope > 0
    slope_negative = slope < 0

    if two_day_above and weekly_above and slope_positive:
        regime = "UPTREND"
    elif two_day_below and weekly_below and slope_negative:
        regime = "DOWNTREND"
    else:
        regime = "SIDEWAYS"

    signals = {
        "DAILY_LAST_COMPLETE": _iso_utc(current["ts"].to_pydatetime()),
        "DAILY_CLOSE": round(float(current["close"]), 8),
        "DAILY_PREVIOUS_CLOSE": round(float(previous["close"]), 8),
        "DAILY_SMA150": round(float(current["sma150"]), 8),
        "DAILY_PREVIOUS_SMA150": round(float(previous["sma150"]), 8),
        "DAILY_EMA20": round(float(current["ema20"]), 8),
        "DAILY_EMA50": round(float(current["ema50"]), 8),
        "DAILY_PREVIOUS_EMA20": round(float(previous["ema20"]), 8),
        "DAILY_PREVIOUS_EMA50": round(float(previous["ema50"]), 8),
        "WEEKLY_LAST_COMPLETE": _iso_utc(weekly_current["ts"].to_pydatetime()),
        "WEEKLY_CLOSE": round(float(weekly_current["close"]), 8),
        "WEEKLY_EMA20": round(float(weekly_current["ema20"]), 8),
        "SMA150_SLOPE_20D": round(slope, 8),
        "TWO_DAY_ABOVE": two_day_above,
        "TWO_DAY_BELOW": two_day_below,
        "WEEKLY_ABOVE": weekly_above,
        "WEEKLY_BELOW": weekly_below,
        "SLOPE_POSITIVE": slope_positive,
        "SLOPE_NEGATIVE": slope_negative,
    }
    return regime, signals


def _time_stats(frame: pd.DataFrame, dates: list[date]) -> list[dict[str, Any]]:
    selected = frame[frame["local_date"].isin(dates)].copy()
    daily_low = selected.groupby("local_date")["low"].transform("min")
    selected["miss_pct"] = (selected["close"] - daily_low) / daily_low * 100
    selected["is_snipe"] = selected["miss_pct"] < 0.5
    stats = []
    for local_time, group in selected.groupby("local_time", sort=True):
        if group["local_date"].nunique() != len(dates):
            continue
        stats.append(
            {
                "TIME": str(local_time),
                "MEDIAN_MISS": float(median(group["miss_pct"].tolist())),
                "WIN_RATE": float(group["is_snipe"].mean() * 100),
                "DAYS": len(dates),
            }
        )
    if not stats:
        raise AnalysisError(f"No complete time-of-day candidates for the {len(dates)}-day window")
    return sorted(stats, key=lambda item: (item["MEDIAN_MISS"], -item["WIN_RATE"], item["TIME"]))


def _rolling_time_stats(frame: pd.DataFrame, days: int) -> list[dict[str, Any]]:
    """Score one deterministic rolling 24-hour window per requested day.

    Calendar-day grouping cannot produce seven full days late in Bangkok from
    Kraken's 720-candle page.  Exact contiguous 96-candle blocks do: every
    block contains each Bangkok HH:MM once, without depending on analysis time.
    """

    expected = days * INTRADAY_CANDLES_PER_DAY
    if len(frame) != expected:
        raise AnalysisError(
            f"The {days}-day timing window requires exactly {expected} candles"
        )
    selected = frame.copy().reset_index(drop=True)
    selected["rolling_day"] = [
        index // INTRADAY_CANDLES_PER_DAY for index in range(expected)
    ]
    rolling_low = selected.groupby("rolling_day")["low"].transform("min")
    selected["miss_pct"] = (selected["close"] - rolling_low) / rolling_low * 100
    selected["is_snipe"] = selected["miss_pct"] < 0.5
    stats: list[dict[str, Any]] = []
    for local_time, group in selected.groupby("local_time", sort=True):
        if len(group) != days or group["rolling_day"].nunique() != days:
            raise AnalysisError(
                f"The {days}-day timing window is missing local time {local_time}"
            )
        stats.append(
            {
                "TIME": str(local_time),
                "MEDIAN_MISS": float(median(group["miss_pct"].tolist())),
                "WIN_RATE": float(group["is_snipe"].mean() * 100),
                "DAYS": days,
            }
        )
    if len(stats) != INTRADAY_CANDLES_PER_DAY:
        raise AnalysisError(
            f"The {days}-day timing window has {len(stats)} local time slots; "
            f"need {INTRADAY_CANDLES_PER_DAY}"
        )
    return sorted(
        stats,
        key=lambda item: (item["MEDIAN_MISS"], -item["WIN_RATE"], item["TIME"]),
    )


def _rounded_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "TIME": item["TIME"],
        "MEDIAN_MISS": round(float(item["MEDIAN_MISS"]), 6),
        "WIN_RATE": round(float(item["WIN_RATE"]), 4),
        "DAYS": int(item["DAYS"]),
    }


def choose_timing_candidate(
    tables: Mapping[int, list[dict[str, Any]]],
) -> tuple[dict[str, Any], int, str, int]:
    """Apply recency, base-window, and deterministic tie-break rules."""

    if any(days not in tables or not tables[days] for days in PERIODS):
        raise AnalysisError("Timing tables must contain non-empty 3, 5, and 7-day windows")
    best3, best5, best7 = (tables[days][0] for days in PERIODS)
    if (
        best3["WIN_RATE"] >= best7["WIN_RATE"] + 10.0
        and best3["MEDIAN_MISS"] <= best7["MEDIAN_MISS"] + 0.20
    ):
        selected_window = 3
        selection_rule = "RECENCY_3D_OVERRIDE"
        initial = best3
    elif best5["MEDIAN_MISS"] <= best7["MEDIAN_MISS"] - 0.15:
        selected_window = 5
        selection_rule = "BASE_5D_MATERIAL_IMPROVEMENT"
        initial = best5
    else:
        selected_window = 7
        selection_rule = "BASE_7D"
        initial = best7

    near_ties = [
        item
        for item in tables[selected_window]
        if item["MEDIAN_MISS"] <= initial["MEDIAN_MISS"] + 0.10
    ]
    top_five_times = {
        days: {item["TIME"] for item in tables[days][:5]} for days in PERIODS
    }
    seven_day_by_time = {item["TIME"]: item for item in tables[7]}

    def tie_key(item: Mapping[str, Any]):
        appearances = sum(item["TIME"] in top_five_times[days] for days in PERIODS)
        seven_day_win_rate = seven_day_by_time.get(item["TIME"], {}).get("WIN_RATE", -1.0)
        return (-appearances, -seven_day_win_rate, item["TIME"])

    selected = min(near_ties, key=tie_key)
    appearances = sum(selected["TIME"] in top_five_times[days] for days in PERIODS)
    return dict(selected), selected_window, selection_rule, appearances


def select_best_time(
    rows: Iterable[Iterable[Any]],
    *,
    now: datetime | None = None,
    local_tz: str = LOCAL_TZ,
) -> tuple[str, dict[str, Any]]:
    """Select a local buy time using the deterministic 3/5/7-day policy."""

    reference = now or _utc_now()
    try:
        zone = ZoneInfo(local_tz)
    except Exception as exc:
        raise AnalysisError(f"Unknown analysis timezone: {local_tz}") from exc
    frame = _completed_frame(
        rows, QUARTER_HOUR_MS, now=reference, label="15-minute market data"
    )
    _require_fresh_candle(
        frame,
        QUARTER_HOUR_MS,
        MAX_INTRADAY_DATA_AGE,
        now=reference,
        label="15-minute market data",
    )
    frame = _contiguous_tail(
        frame,
        INTRADAY_HISTORY_CANDLES,
        QUARTER_HOUR_MS,
        label="15-minute market data",
    )
    frame["local_ts"] = frame["ts"].dt.tz_convert(zone)
    frame["local_time"] = frame["local_ts"].dt.strftime("%H:%M")

    tables = {
        days: _rolling_time_stats(
            frame.tail(days * INTRADAY_CANDLES_PER_DAY).copy(), days
        )
        for days in PERIODS
    }
    selected, selected_window, selection_rule, appearances = choose_timing_candidate(tables)
    timing = {
        "ANALYZED_AT": _iso_utc(reference),
        "TIMEZONE": local_tz,
        "SELECTED_LOCAL_TIME": selected["TIME"],
        "SELECTED_WINDOW_DAYS": selected_window,
        "SELECTION_RULE": selection_rule,
        "TOP_FIVE_APPEARANCES": appearances,
        "SELECTED_METRICS": _rounded_candidate(selected),
        "HISTORY_CANDLES": INTRADAY_HISTORY_CANDLES,
        "HISTORY_START": _iso_utc(frame.iloc[0]["ts"].to_pydatetime()),
        "HISTORY_END": _iso_utc(frame.iloc[-1]["ts"].to_pydatetime()),
        "WINDOWS": {
            str(days): {
                "BEST": _rounded_candidate(tables[days][0]),
                "TOP_5": [_rounded_candidate(item) for item in tables[days][:5]],
            }
            for days in PERIODS
        },
    }
    return selected["TIME"], timing


def analyze_period(df: pd.DataFrame, days: int, local_tz: str):
    """Compatibility view of deterministic per-time statistics.

    New integrations should call :func:`select_best_time`; this helper preserves
    the legacy report tuple without participating in final selection.
    """

    if days not in PERIODS:
        raise ValueError("days must be one of 3, 5, or 7")
    period = df.copy()
    if "local_date" not in period or "local_time" not in period:
        period["local_ts"] = period["ts"].dt.tz_convert(local_tz)
        period["local_date"] = period["local_ts"].dt.date
        period["local_time"] = period["local_ts"].dt.strftime("%H:%M")
    dates = sorted(period["local_date"].unique())[-days:]
    stats = _time_stats(period, dates)
    top_dca = pd.DataFrame(
        {
            "time": [item["TIME"] for item in stats[:5]],
            "median_miss": [item["MEDIAN_MISS"] for item in stats[:5]],
            "win_rate": [item["WIN_RATE"] for item in stats[:5]],
        }
    )
    lows = period[period["local_date"].isin(dates)].loc[
        lambda data: data.groupby("local_date")["low"].idxmin()
    ]
    common = (
        lows["local_time"].value_counts().rename_axis("time").reset_index(name="days_won")
    )
    common["share"] = common["days_won"] / len(dates)
    average = (
        period[period["local_date"].isin(dates)]
        .groupby("local_time")["low"]
        .mean()
        .sort_values()
        .head(5)
        .rename_axis("time")
        .reset_index(name="avg_low")
    )
    selected = period[period["local_date"].isin(dates)]
    return common.head(5), average, top_dca, selected["ts"].min(), selected["ts"].max()


def next_execution_time(
    selected_local_time: str,
    *,
    analyzed_at: datetime,
    local_tz: str = LOCAL_TZ,
    minimum_notice: timedelta = timedelta(minutes=30),
) -> datetime:
    """Return the next occurrence at least 30 minutes after analysis."""

    if analyzed_at.tzinfo is None or analyzed_at.utcoffset() is None:
        raise AnalysisError("analyzed_at must include a timezone")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", selected_local_time):
        raise AnalysisError("Selected local time must be HH:MM")
    zone = ZoneInfo(local_tz)
    local_now = analyzed_at.astimezone(zone)
    hour, minute = (int(value) for value in selected_local_time.split(":"))
    candidate = datetime.combine(local_now.date(), clock_time(hour, minute), tzinfo=zone)
    if candidate < local_now + minimum_notice:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _fetch_asset_rows(exchange, symbol: str) -> tuple[list, list, list]:
    daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=260)
    weekly = exchange.fetch_ohlcv(symbol, timeframe="1w", limit=40)
    intraday = fetch_ohlcv_last_n_days(exchange, symbol, "15m", 7)
    return daily, weekly, intraday


def _decision_id(target: str, generated_at: datetime, fingerprint: str) -> str:
    # UUID suffix ensures rerunning within the same second still invalidates an
    # earlier failed/ready decision rather than accidentally reusing it.
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{target.lower()}-{stamp}-{fingerprint[:8]}-{uuid4().hex[:8]}"


def analyze_asset(
    exchange,
    target: str,
    rule: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one complete READY decision or raise a fail-closed error."""

    if target not in TARGET_KEYS:
        raise AnalysisError(f"Unsupported target: {target}")
    generated = now or _utc_now()
    fingerprint = rules_hash(target, rule)
    daily, weekly, intraday = _fetch_asset_rows(exchange, TARGET_SYMBOLS[target])
    regime, signals = classify_trend(daily, weekly, now=generated)
    selected_time, timing = select_best_time(intraday, now=generated, local_tz=LOCAL_TZ)
    execute_at = next_execution_time(
        selected_time, analyzed_at=generated, local_tz=LOCAL_TZ
    )
    amount_tier = "UP" if regime == "UPTREND" else "LOW"
    return {
        "STATUS": READY_STATUS,
        "REGIME": regime,
        "AMOUNT_TIER": amount_tier,
        "EXECUTE_AT": _iso_utc(execute_at),
        "VALID_UNTIL": _iso_utc(execute_at + timedelta(minutes=60)),
        "DECISION_ID": _decision_id(target, generated, fingerprint),
        "RULES_HASH": fingerprint,
        "SIGNALS": signals,
        "TIMING": timing,
    }


def error_decision(
    target: str,
    rule: Mapping[str, Any],
    error: Exception | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a fresh ERROR decision that can never be executed."""

    generated = now or _utc_now()
    fingerprint = rules_hash(target, rule)
    message = str(error).strip() or "Unknown analysis failure"
    return {
        "STATUS": ERROR_STATUS,
        "REGIME": None,
        "AMOUNT_TIER": None,
        "EXECUTE_AT": None,
        "VALID_UNTIL": None,
        "DECISION_ID": _decision_id(target, generated, fingerprint),
        "RULES_HASH": fingerprint,
        "SIGNALS": {"ERROR": message},
        "TIMING": {"ANALYZED_AT": _iso_utc(generated), "ERROR": message},
    }


def get_ai_summary(deterministic_report: str, current_symbol: str):
    """Ask Gemini to explain, never choose, the deterministic result."""

    if not GEMINI_API_KEY:
        return "Gemini explanation unavailable; deterministic decision unchanged.", None, None
    prompt = f"""
Explain the deterministic Kraken analysis for {current_symbol} in no more than
three sentences. Do not recommend or select a different regime, amount, asset,
or execution time. Do not invent metrics. The Python decision is final.

{deterministic_report}
""".strip()
    for model_name in ("gemini-2.5-flash-lite", "gemini-2.5-flash"):
        try:
            with genai.Client(api_key=GEMINI_API_KEY) as client:
                response = client.models.generate_content(model=model_name, contents=prompt)
            explanation = (response.text or "").strip()
            if explanation:
                return explanation, None, model_name
        except Exception:  # optional narration must never fail analysis
            continue
    return "Gemini explanation unavailable; deterministic decision unchanged.", None, None


def _decision_report(target: str, decision: Mapping[str, Any], rule: Mapping[str, Any]) -> str:
    if decision["STATUS"] == ERROR_STATUS:
        return (
            f"❌ **{target} analysis ERROR**\n"
            f"Purchase skipped. {decision['SIGNALS'].get('ERROR', 'Unknown error')}"
        )
    tier = decision["AMOUNT_TIER"]
    amount = rule["REGIME_AMOUNTS_GBP"][tier]
    timing = decision["TIMING"]
    return (
        f"📊 **{target} daily decision**\n"
        f"Regime: `{decision['REGIME']}` → `{tier}` tier (`£{amount:g}` configured)\n"
        f"Best time: `{timing['SELECTED_LOCAL_TIME']} {timing['TIMEZONE']}` "
        f"via `{timing['SELECTION_RULE']}`\n"
        f"Execution: `{decision['EXECUTE_AT']}` to `{decision['VALID_UNTIL']}`\n"
        f"Trading: `{'enabled' if rule['BUY_ENABLED'] else 'disabled'}`"
    )


def send_to_discord(report_content: str, color: int = 3_447_003) -> bool:
    """Post a bounded Discord embed without leaking serialized configuration."""

    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook is not configured; analysis notification skipped.")
        return False
    chunks = [report_content[index : index + 3900] for index in range(0, len(report_content), 3900)]
    try:
        for chunk in chunks:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"embeds": [{"description": chunk, "color": color}]},
                timeout=10,
            )
            response.raise_for_status()
        return True
    except requests.RequestException as exc:
        # A requests exception can embed the webhook URL (which is a secret).
        print(f"Discord analysis notification failed ({type(exc).__name__}).")
        return False


def persist_analysis_state(state: Mapping[str, Any]) -> bool:
    """Persist state directly when GitHub credentials are explicitly provided.

    GitHub Actions can alternatively consume the ``analysis_state`` output.  No
    serialized state is printed to logs.
    """

    token = os.environ.get("GH_PAT_FOR_VARS") or os.environ.get("GH_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        return False
    value = json.dumps(state, separators=(",", ":"), sort_keys=True)
    base_url = f"https://api.github.com/repos/{repository}/actions/variables"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.patch(
        f"{base_url}/{ANALYSIS_VARIABLE}",
        headers=headers,
        json={"name": ANALYSIS_VARIABLE, "value": value},
        timeout=20,
    )
    if response.status_code == 404:
        response = requests.post(
            base_url,
            headers=headers,
            json={"name": ANALYSIS_VARIABLE, "value": value},
            timeout=20,
        )
    response.raise_for_status()
    return True


def _write_actions_output(state: Mapping[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    compact = json.dumps(state, separators=(",", ":"), sort_keys=True)
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"analysis_state={compact}\n")


def _existing_or_empty_state(rules: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    if DCA_ANALYSIS_STATE_ENV.strip():
        try:
            state = validate_analysis_state(DCA_ANALYSIS_STATE_ENV, require_all=True)
            for target in TARGET_KEYS:
                if state["TARGETS"][target]["RULES_HASH"] != rules_hash(
                    target, rules[target]
                ):
                    state["TARGETS"][target] = error_decision(
                        target,
                        rules[target],
                        "Live budgets changed; fresh analysis required",
                        now=now,
                    )
            return state
        except ValueError as exc:
            print(f"Existing analysis state is invalid and will be replaced safely: {exc}")
    return empty_analysis_state(rules, now=now)


def main() -> int:
    """Analyze selected targets, persist four decisions, and alert per asset."""

    generated = _utc_now()
    try:
        rules = validate_rules_map(DCA_TARGET_MAP_ENV)
        symbols = _parse_symbols(SYMBOLS_ENV, DCA_TARGET_MAP_ENV)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        send_to_discord(f"❌ **DCA analysis configuration error**\n{exc}", color=15_148_332)
        return 1

    selected_targets = [symbol.replace("/", "_") for symbol in symbols]
    state = _existing_or_empty_state(rules, generated)
    state["VERSION"] = ANALYSIS_STATE_VERSION
    state["GENERATED_AT"] = _iso_utc(generated)
    exchange = get_analysis_exchange()
    had_error = False

    for target in selected_targets:
        try:
            decision = analyze_asset(exchange, target, rules[target], now=generated)
            report = _decision_report(target, decision, rules[target])
            explanation, _, model = get_ai_summary(report, TARGET_SYMBOLS[target])
            if model:
                report += f"\nGemini explanation ({model}): {explanation}"
            print(
                f"{target}: READY regime={decision['REGIME']} "
                f"time={decision['TIMING']['SELECTED_LOCAL_TIME']} {LOCAL_TZ}"
            )
        except Exception as exc:
            had_error = True
            decision = error_decision(target, rules[target], exc, now=generated)
            report = _decision_report(target, decision, rules[target])
            print(f"{target}: ERROR {exc}")
        state["TARGETS"][target] = decision
        send_to_discord(report, color=15_148_332 if decision["STATUS"] == ERROR_STATUS else 3_447_003)

    # Structural validation happens before either persistence route.
    validated = validate_analysis_state(state, rules)
    persisted = persist_analysis_state(validated)
    _write_actions_output(validated)
    print(
        f"Analysis complete: {len(selected_targets)} target(s), "
        f"state persistence={'direct' if persisted else 'workflow output'}; "
        f"errors={'yes' if had_error else 'no'}."
    )
    # A per-asset ERROR is successfully persisted fail-closed state, not a
    # workflow crash. The Discord alert and STATUS=ERROR are the operational signal.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

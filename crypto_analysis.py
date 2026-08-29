"""Deterministic Kraken GBP-market regime and execution-time analysis.

Gemini is an optional narrator only.  All spend-affecting outputs (regime,
amount tier, and execution time) are calculated locally from completed Kraken
candles and are persisted as ``DCA_ANALYSIS_STATE``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time as clock_time, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable, Mapping
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
    TIMING_POLICY_VERSION,
    UPTREND_CONFIRMATION_CANDLES,
    UPTREND_OVERRIDE_STATE_VERSION,
    analysis_decision_matches_uptrend_override,
    amount_tier_for_regime,
    default_rules_map,
    effective_amount,
    empty_analysis_state,
    rules_hash,
    validate_analysis_state,
    validate_rules_map,
    validate_uptrend_override_state,
)
from kraken_history import HistoryError, load_ready_history


EXCHANGE_ID = os.environ.get("EXCHANGE_ID", "kraken")
SYMBOLS_ENV = os.environ.get("SYMBOL", "")
LOCAL_TZ = os.environ.get("TIMEZONE", "Asia/Bangkok")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DCA_TARGET_MAP_ENV = os.environ.get("DCA_TARGET_MAP", "{}")
DCA_ANALYSIS_STATE_ENV = os.environ.get("DCA_ANALYSIS_STATE", "")
DCA_UPTREND_OVERRIDE_STATE_ENV = os.environ.get(
    "DCA_UPTREND_OVERRIDE_STATE", ""
)
ANALYSIS_VARIABLE = "DCA_ANALYSIS_STATE"
UPTREND_OVERRIDE_VARIABLE = "DCA_UPTREND_OVERRIDE_STATE"
TARGET_MIGRATION_LOCK_VARIABLE = "DCA_TARGET_MIGRATION_LOCK"
PERIODS = (3, 5, 7, 14, 30, 45, 60)
CORE_PERIODS = (14, 30, 45, 60)
DCA_TRADING_MODE = os.environ.get("DCA_TRADING_MODE", "shadow").strip().lower()
DCA_CANARY_SYMBOL = os.environ.get("DCA_CANARY_SYMBOL", "SOL_GBP").strip().upper()

DAILY_TIMEFRAME_MS = 24 * 60 * 60 * 1000
WEEKLY_TIMEFRAME_MS = 7 * DAILY_TIMEFRAME_MS
QUARTER_HOUR_MS = 15 * 60 * 1000
SMA150_PERIOD = 150
# SMA150's 20-day slope needs the current SMA and the value 20 completed
# candles earlier: 150 + 20 = 170 completed daily candles. Requiring more
# would exclude otherwise analyzable newer markets without adding another
# signal to the documented regime policy.
MIN_DAILY_CANDLES = 170
MIN_WEEKLY_CANDLES = 20
KRAKEN_OHLCV_LIMIT = 720
INTRADAY_CANDLES_PER_DAY = 96
INTRADAY_HISTORY_CANDLES = 60 * INTRADAY_CANDLES_PER_DAY
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
        candidate = {"BTC": "BTC/GBP", "ETH": "ETH/GBP", "SOL": "SOL/GBP"}.get(
            candidate, candidate
        )
    target = candidate.replace("/", "_")
    if target not in TARGET_KEYS:
        raise ValueError(
            f"Only BTC/GBP, ETH/GBP, and SOL/GBP are supported: {value}"
        )
    return target


def _parse_symbols(symbols_env: str, dca_map_env: str) -> list[str]:
    """Return the deterministic mixed Kraken pair strings."""

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
            raise ValueError("SYMBOL did not contain a supported Kraken DCA pair")
        return [TARGET_SYMBOLS[target] for target in targets]

    try:
        rules = validate_rules_map(dca_map_env)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return [TARGET_SYMBOLS[target] for target in TARGET_KEYS if target in rules]


def get_analysis_exchange(exchange_id: str = EXCHANGE_ID):
    """Create the one supported public market-data client."""

    if str(exchange_id).strip().lower() != "kraken":
        raise ValueError("Crypto analysis supports the configured Kraken markets only")
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
    # Indicators and the three-candle confirmation must never bridge a missing
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

    daily["sma150"] = daily["close"].rolling(
        SMA150_PERIOD, min_periods=SMA150_PERIOD
    ).mean()
    daily["ema20"] = daily["close"].ewm(span=20, adjust=False).mean()
    daily["ema50"] = daily["close"].ewm(span=50, adjust=False).mean()
    weekly["ema20"] = weekly["close"].ewm(span=20, adjust=False).mean()

    current = daily.iloc[-1]
    previous = daily.iloc[-2]
    two_days_ago = daily.iloc[-3]
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
    three_day_below = bool(
        current["close"] < current["sma150"]
        and previous["close"] < previous["sma150"]
        and two_days_ago["close"] < two_days_ago["sma150"]
    )
    weekly_above = bool(weekly_current["close"] > weekly_current["ema20"])
    weekly_below = bool(weekly_current["close"] < weekly_current["ema20"])
    slope = float(slope_end - slope_start)
    slope_positive = slope > 0
    slope_negative = slope < 0
    confirmation = daily.tail(UPTREND_CONFIRMATION_CANDLES)
    confirmation_above = confirmation["close"] > confirmation["sma150"]
    confirmation_count = 0
    for is_above in reversed(confirmation_above.tolist()):
        if not bool(is_above):
            break
        confirmation_count += 1
    uptrend_confirmed = bool(confirmation_above.all())

    if uptrend_confirmed:
        regime = "UPTREND"
    elif three_day_below and current["ema20"] < current["ema50"]:
        regime = "DOWNTREND"
    else:
        regime = "SIDEWAYS"

    signals = {
        "DAILY_LAST_COMPLETE": _iso_utc(current["ts"].to_pydatetime()),
        "DAILY_CLOSE": round(float(current["close"]), 8),
        "DAILY_PREVIOUS_CLOSE": round(float(previous["close"]), 8),
        "DAILY_TWO_DAYS_AGO_CLOSE": round(float(two_days_ago["close"]), 8),
        "DAILY_SMA150": round(float(current["sma150"]), 8),
        "DAILY_PREVIOUS_SMA150": round(float(previous["sma150"]), 8),
        "DAILY_TWO_DAYS_AGO_SMA150": round(
            float(two_days_ago["sma150"]), 8
        ),
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
        "THREE_DAY_BELOW": three_day_below,
        "WEEKLY_ABOVE": weekly_above,
        "WEEKLY_BELOW": weekly_below,
        "SLOPE_POSITIVE": slope_positive,
        "SLOPE_NEGATIVE": slope_negative,
        "UPTREND_CONFIRMATION_REQUIRED": UPTREND_CONFIRMATION_CANDLES,
        "UPTREND_CONFIRMATION_COUNT": confirmation_count,
        "UPTREND_CONFIRMED": uptrend_confirmed,
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
        raise AnalysisError(
            "Timing tables must contain non-empty 3, 5, 7, 14, 30, 45, and 60-day windows"
        )
    best14, best30, _best45, best60 = (
        tables[days][0] for days in CORE_PERIODS
    )
    if (
        best14["WIN_RATE"] >= best30["WIN_RATE"] + 10.0
        and best14["MEDIAN_MISS"] <= best30["MEDIAN_MISS"] + 0.20
    ):
        selected_window = 14
        selection_rule = "RECENCY_14D_OVERRIDE"
        initial = best14
    elif best30["MEDIAN_MISS"] <= best60["MEDIAN_MISS"] - 0.15:
        selected_window = 30
        selection_rule = "BASE_30D_MATERIAL_IMPROVEMENT"
        initial = best30
    else:
        selected_window = 60
        selection_rule = "BASE_60D"
        initial = best60

    near_ties = [
        item
        for item in tables[selected_window]
        if item["MEDIAN_MISS"] <= initial["MEDIAN_MISS"] + 0.10
    ]
    # Short windows participate in consistency without being allowed to
    # override the stable 14/30/60 threshold policy on their own.
    consistency_periods = PERIODS
    top_five_times = {
        days: {item["TIME"] for item in tables[days][:5]}
        for days in consistency_periods
    }
    sixty_day_by_time = {item["TIME"]: item for item in tables[60]}

    def tie_key(item: Mapping[str, Any]):
        appearances = sum(
            item["TIME"] in top_five_times[days] for days in consistency_periods
        )
        sixty_day_win_rate = sixty_day_by_time.get(item["TIME"], {}).get(
            "WIN_RATE", -1.0
        )
        return (-appearances, -sixty_day_win_rate, item["TIME"])

    selected = min(near_ties, key=tie_key)
    appearances = sum(
        selected["TIME"] in top_five_times[days] for days in consistency_periods
    )
    return dict(selected), selected_window, selection_rule, appearances


def select_best_time(
    rows: Iterable[Iterable[Any]],
    *,
    now: datetime | None = None,
    local_tz: str = LOCAL_TZ,
) -> tuple[str, dict[str, Any]]:
    """Select a local buy time using all seven deterministic windows."""

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
    frame["local_ts"] = frame["ts"].dt.tz_convert(zone)
    frame["local_date"] = frame["local_ts"].dt.date
    frame["local_time"] = frame["local_ts"].dt.strftime("%H:%M")
    current_date = reference.astimezone(zone).date()
    complete_dates = sorted(
        value for value in frame["local_date"].unique() if value < current_date
    )
    if len(complete_dates) < 60:
        raise AnalysisError(
            f"15-minute market data has {len(complete_dates)} complete Bangkok days; need 60"
        )
    selected_dates = complete_dates[-60:]
    frame = frame[frame["local_date"].isin(selected_dates)].copy()
    tables = {
        days: _time_stats(frame, selected_dates[-days:])
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
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "HISTORY_CANDLES": len(frame),
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
        raise ValueError("days must be one of 3, 5, 7, 14, 30, 45, or 60")
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
    minimum_notice: timedelta = timedelta(0),
) -> datetime:
    """Return today's selected time or the bounded 05:00 legacy catch-up.

    The original system analyzed at 04:00 Bangkok and bought at 05:00 when a
    selected time had already passed.  This preserves that explicitly without
    rolling the decision into tomorrow or allowing an all-day catch-up.
    """

    if analyzed_at.tzinfo is None or analyzed_at.utcoffset() is None:
        raise AnalysisError("analyzed_at must include a timezone")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", selected_local_time):
        raise AnalysisError("Selected local time must be HH:MM")
    zone = ZoneInfo(local_tz)
    local_now = analyzed_at.astimezone(zone)
    hour, minute = (int(value) for value in selected_local_time.split(":"))
    candidate = datetime.combine(local_now.date(), clock_time(hour, minute), tzinfo=zone)
    if candidate < local_now + minimum_notice:
        candidate = datetime.combine(
            local_now.date(), clock_time(5, 0), tzinfo=zone
        )
    return candidate.astimezone(timezone.utc)


def _fetch_asset_rows(exchange, symbol: str) -> tuple[list, list, list, dict[str, Any]]:
    daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=260)
    weekly = exchange.fetch_ohlcv(symbol, timeframe="1w", limit=40)
    target = symbol.replace("/", "_")
    intraday, history = load_ready_history(target)
    return daily, weekly, intraday, history


def _decision_id(
    target: str,
    analysis_date: str,
    fingerprint: str,
    history_hash: str,
) -> str:
    payload = "|".join(
        (target, analysis_date, TIMING_POLICY_VERSION, fingerprint, history_hash)
    )
    digest = sha256(payload.encode("utf-8")).hexdigest()
    return f"{target.lower()}-{analysis_date.replace('-', '')}-{digest[:16]}"


def _execution_status(target: str, rule: Mapping[str, Any], execute_at: datetime, now: datetime) -> str:
    if not rule["BUY_ENABLED"]:
        return "DISABLED"
    if execute_at + timedelta(minutes=60) < now.astimezone(timezone.utc):
        return "EXPIRED"
    if DCA_TRADING_MODE == "shadow":
        return "SHADOW"
    if DCA_TRADING_MODE == "canary" and target != DCA_CANARY_SYMBOL:
        return "SHADOW"
    if DCA_TRADING_MODE in {"canary", "live"}:
        return "ARMED"
    return "BLOCKED"


def parse_uptrend_override_state(
    value: str | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return strict override state, treating only an omitted value as empty."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return {"VERSION": UPTREND_OVERRIDE_STATE_VERSION, "TARGETS": {}}
    return validate_uptrend_override_state(value)


def _apply_uptrend_override(
    regime: str,
    signals: Mapping[str, Any],
    override: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Apply one already-validated override without hiding the normal regime."""

    annotated = _uptrend_override_audit_signals(
        regime,
        signals,
        override,
        applied=bool(override and override["ACTIVE"] and regime != "UPTREND"),
    )
    return ("UPTREND" if annotated["UPTREND_OVERRIDE_ACTIVE"] else regime), annotated


def _uptrend_override_audit_signals(
    regime: str | None,
    signals: Mapping[str, Any],
    override: Mapping[str, Any] | None,
    *,
    applied: bool,
) -> dict[str, Any]:
    """Attach validated override evidence without making a failed analysis tradable."""

    entry = dict(override) if override is not None else None
    active = bool(entry and entry["ACTIVE"])
    return {
        **dict(signals),
        "REGIME_WITHOUT_OVERRIDE": regime,
        "UPTREND_OVERRIDE_ACTIVE": active,
        "UPTREND_OVERRIDE_APPLIED": bool(active and applied),
        "UPTREND_OVERRIDE_REASON": entry["REASON"] if entry else None,
        "UPTREND_OVERRIDE_ACTIVATED_AT": entry["ACTIVATED_AT"] if entry else None,
        "UPTREND_OVERRIDE_RELEASED_AT": entry["RELEASED_AT"] if entry else None,
        "UPTREND_OVERRIDE_AUTO_RELEASED": False,
    }


def _release_confirmed_uptrend_override(
    override_state: dict[str, Any],
    target: str,
    decision: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    """Release an active target override after the normal rule confirms UPTREND."""

    entry = override_state["TARGETS"].get(target)
    signals = decision.get("SIGNALS")
    if (
        not isinstance(entry, Mapping)
        or entry.get("ACTIVE") is not True
        or not isinstance(signals, Mapping)
        or signals.get("UPTREND_CONFIRMED") is not True
    ):
        return False

    released_at = _iso_utc(now)
    released_entry = {
        **dict(entry),
        "ACTIVE": False,
        "RELEASED_AT": released_at,
    }
    override_state["TARGETS"][target] = released_entry
    decision["SIGNALS"] = {
        **dict(signals),
        "UPTREND_OVERRIDE_ACTIVE": False,
        "UPTREND_OVERRIDE_APPLIED": False,
        "UPTREND_OVERRIDE_RELEASED_AT": released_at,
        "UPTREND_OVERRIDE_AUTO_RELEASED": True,
    }
    return True


def analyze_asset(
    exchange,
    target: str,
    rule: Mapping[str, Any],
    *,
    now: datetime | None = None,
    uptrend_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one complete READY decision or raise a fail-closed error."""

    if target not in TARGET_KEYS:
        raise AnalysisError(f"Unsupported target: {target}")
    generated = now or _utc_now()
    fingerprint = rules_hash(target, rule)
    daily, weekly, intraday, history = _fetch_asset_rows(exchange, TARGET_SYMBOLS[target])
    normal_regime, signals = classify_trend(daily, weekly, now=generated)
    regime, signals = _apply_uptrend_override(
        normal_regime, signals, uptrend_override
    )
    selected_time, timing = select_best_time(intraday, now=generated, local_tz=LOCAL_TZ)
    zone = ZoneInfo(LOCAL_TZ)
    local_now = generated.astimezone(zone)
    hour, minute = (int(value) for value in selected_time.split(":"))
    selected_at = datetime.combine(
        local_now.date(), clock_time(hour, minute), tzinfo=zone
    ).astimezone(timezone.utc)
    execute_at = next_execution_time(
        selected_time, analyzed_at=generated, local_tz=LOCAL_TZ
    )
    catchup_applied = execute_at != selected_at
    analysis_date = local_now.date().isoformat()
    amount_tier = amount_tier_for_regime(regime)
    return {
        "ENABLED": bool(rule["BUY_ENABLED"]),
        "ANALYSIS_STATUS": READY_STATUS,
        "EXECUTION_STATUS": _execution_status(target, rule, execute_at, generated),
        "REGIME": regime,
        "AMOUNT_TIER": amount_tier,
        "SELECTED_AT": _iso_utc(selected_at),
        "EXECUTE_AT": _iso_utc(execute_at),
        "VALID_UNTIL": _iso_utc(execute_at + timedelta(minutes=60)),
        "CATCHUP_APPLIED": catchup_applied,
        "DECISION_ID": _decision_id(
            target, analysis_date, fingerprint, history["HASH"]
        ),
        "RULES_HASH": fingerprint,
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": analysis_date,
        "HISTORY": history,
        "SIGNALS": signals,
        "TIMING": timing,
        "ERROR": None,
    }


def error_decision(
    target: str,
    rule: Mapping[str, Any],
    error: Exception | str,
    *,
    now: datetime | None = None,
    uptrend_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fresh ERROR decision that can never be executed."""

    generated = now or _utc_now()
    fingerprint = rules_hash(target, rule)
    message = str(error).strip() or "Unknown analysis failure"
    analysis_status = "HISTORY_NOT_READY" if isinstance(error, HistoryError) else ERROR_STATUS
    analysis_date = generated.astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()
    history = {
        "STATUS": "HISTORY_NOT_READY" if analysis_status == "HISTORY_NOT_READY" else "ERROR"
    }
    signals = _uptrend_override_audit_signals(
        None,
        {"ERROR": message},
        uptrend_override,
        applied=False,
    )
    return {
        "ENABLED": bool(rule["BUY_ENABLED"]),
        "ANALYSIS_STATUS": analysis_status,
        "EXECUTION_STATUS": "DISABLED" if not rule["BUY_ENABLED"] else "BLOCKED",
        "REGIME": None,
        "AMOUNT_TIER": None,
        "SELECTED_AT": None,
        "EXECUTE_AT": None,
        "VALID_UNTIL": None,
        "CATCHUP_APPLIED": False,
        "DECISION_ID": _decision_id(target, analysis_date, fingerprint, "0" * 64),
        "RULES_HASH": fingerprint,
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": analysis_date,
        "HISTORY": history,
        "SIGNALS": signals,
        "TIMING": {"ANALYZED_AT": _iso_utc(generated), "ERROR": message},
        "ERROR": message,
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
    # Prefer Google's current GA, free-tier Flash-Lite model.  Keep a second
    # supported GA model as a resilience fallback; narration is non-authoritative.
    for model_name in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
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
    if decision["ANALYSIS_STATUS"] != READY_STATUS:
        signals = decision.get("SIGNALS")
        override_line = ""
        if (
            isinstance(signals, Mapping)
            and signals.get("UPTREND_OVERRIDE_ACTIVE") is True
        ):
            override_line = (
                "\nEmergency override: `ACTIVE` (audit only); orders remain "
                "blocked until analysis is READY."
            )
        return (
            f"❌ **{target} analysis ERROR**\n"
            f"Purchase skipped. {decision.get('ERROR') or 'Unknown error'}"
            f"{override_line}"
        )
    tier = decision["AMOUNT_TIER"]
    amount = effective_amount(rule, decision)
    timing = decision["TIMING"]
    history = decision["HISTORY"]
    signals = decision.get("SIGNALS")
    catchup = " (05:00 catch-up)" if decision["CATCHUP_APPLIED"] else ""
    override_line = ""
    if isinstance(signals, Mapping):
        if signals.get("UPTREND_OVERRIDE_ACTIVE") is True:
            override_line = (
                "Emergency override: `ACTIVE`; normal regime "
                f"`{signals.get('REGIME_WITHOUT_OVERRIDE', 'unknown')}`; "
                "confirmation "
                f"`{signals.get('UPTREND_CONFIRMATION_COUNT', 'unknown')}/"
                f"{signals.get('UPTREND_CONFIRMATION_REQUIRED', 'unknown')}`\n"
            )
        elif signals.get("UPTREND_OVERRIDE_AUTO_RELEASED") is True:
            override_line = (
                "Emergency override: `AUTO-RELEASED`; natural "
                f"{UPTREND_CONFIRMATION_CANDLES}-close UPTREND confirmed\n"
            )
    return (
        f"📊 **{target} daily decision**\n"
        f"Regime: `{decision['REGIME']}` → `{tier}` tier (`£{amount:g}` configured)\n"
        f"{override_line}"
        f"Best time: `{timing['SELECTED_LOCAL_TIME']} {timing['TIMEZONE']}` "
        f"via `{timing['SELECTION_RULE']}`\n"
        f"Effective execution: `{decision['EXECUTE_AT']}`{catchup}; valid to `{decision['VALID_UNTIL']}`\n"
        f"History: `{history.get('FROM')} to {history.get('THROUGH')}`; hash `{history.get('HASH', '')[:12]}`\n"
        f"Execution status: `{decision['EXECUTION_STATUS']}`; decision `{decision['DECISION_ID']}`"
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


def _load_live_uptrend_override_state(
    *,
    operation: str,
) -> dict[str, Any] | None:
    """Read current override state, or return None without repository access."""

    token = os.environ.get("GH_PAT_FOR_VARS") or os.environ.get("GH_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        return None
    base_url = f"https://api.github.com/repos/{repository}/actions/variables"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.get(
        f"{base_url}/{UPTREND_OVERRIDE_VARIABLE}",
        headers=headers,
        timeout=20,
    )
    if response.status_code == 404:
        return {"VERSION": UPTREND_OVERRIDE_STATE_VERSION, "TARGETS": {}}
    if response.status_code != 200:
        raise RuntimeError(
            f"UPTREND override state could not be re-checked before {operation}"
        )
    try:
        return parse_uptrend_override_state(response.json()["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"UPTREND override state is invalid before {operation}"
        ) from exc


def _persist_repository_variable(
    name: str,
    state: Mapping[str, Any],
    *,
    operation: str,
    expected_uptrend_override_state: Mapping[str, Any] | None = None,
) -> bool:
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
    lock_response = requests.get(
        f"{base_url}/{TARGET_MIGRATION_LOCK_VARIABLE}",
        headers=headers,
        timeout=20,
    )
    if lock_response.status_code == 200:
        if operation == "analysis persistence":
            raise RuntimeError("Target migration lock blocks analysis persistence")
        raise RuntimeError(f"Target migration lock blocks {operation}")
    if lock_response.status_code != 404:
        raise RuntimeError(
            f"Target migration lock could not be checked before {operation}"
        )
    if expected_uptrend_override_state is not None:
        expected_override = validate_uptrend_override_state(
            expected_uptrend_override_state
        )
        current_state = _load_live_uptrend_override_state(operation=operation)
        if current_state is None:
            raise RuntimeError(
                f"UPTREND override state could not be re-checked before {operation}"
            )
        if current_state != expected_override:
            raise RuntimeError(
                "UPTREND override state changed during analysis; state write aborted"
            )
    response = requests.patch(
        f"{base_url}/{name}",
        headers=headers,
        json={"name": name, "value": value},
        timeout=20,
    )
    if response.status_code == 404 and name != UPTREND_OVERRIDE_VARIABLE:
        response = requests.post(
            base_url,
            headers=headers,
            json={"name": name, "value": value},
            timeout=20,
        )
    response.raise_for_status()
    return True


def persist_uptrend_override_state(
    state: Mapping[str, Any],
    *,
    expected_state: Mapping[str, Any],
) -> bool:
    """Persist a release only if the live override still matches analysis input."""

    validated = validate_uptrend_override_state(state)
    expected = validate_uptrend_override_state(expected_state)
    return _persist_repository_variable(
        UPTREND_OVERRIDE_VARIABLE,
        validated,
        operation="UPTREND override persistence",
        expected_uptrend_override_state=expected,
    )


def persist_analysis_state(
    state: Mapping[str, Any],
    *,
    expected_uptrend_override_state: Mapping[str, Any] | None = None,
) -> bool:
    """Persist state directly when GitHub credentials are explicitly provided.

    GitHub Actions can alternatively consume the ``analysis_state`` output.  No
    serialized state is printed to logs.
    """

    return _persist_repository_variable(
        ANALYSIS_VARIABLE,
        state,
        operation="analysis persistence",
        expected_uptrend_override_state=expected_uptrend_override_state,
    )


def _write_actions_output(state: Mapping[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    compact = json.dumps(state, separators=(",", ":"), sort_keys=True)
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"analysis_state={compact}\n")


def _persist_analysis_after_override_release(
    analysis_state: Mapping[str, Any],
    uptrend_override_state: Mapping[str, Any],
    *,
    override_released: bool,
    pre_analysis_uptrend_override_state: Mapping[str, Any],
) -> bool:
    """Persist any release first, then the analysis state that observes it."""

    if override_released and not persist_uptrend_override_state(
        uptrend_override_state,
        expected_state=pre_analysis_uptrend_override_state,
    ):
        raise RuntimeError(
            "Released UPTREND override state requires direct repository persistence"
        )
    return persist_analysis_state(
        analysis_state,
        expected_uptrend_override_state=uptrend_override_state,
    )


def _existing_or_empty_state(
    rules: Mapping[str, Any],
    now: datetime,
    uptrend_override_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
                        uptrend_override=(uptrend_override_state or {})
                        .get("TARGETS", {})
                        .get(target),
                    )
            return state
        except ValueError as exc:
            print(f"Existing analysis state is invalid and will be replaced safely: {exc}")
    return empty_analysis_state(rules, now=now)


def _analysis_is_complete_for_live_rules(
    state: Mapping[str, Any],
    rules: Mapping[str, Any],
    selected_targets: Iterable[str],
    analysis_date: str,
    uptrend_override_state: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return true only when a no-op also observes the newest daily candle."""

    reference = now or _utc_now()
    utc_reference = reference.astimezone(timezone.utc)
    latest_completed_daily = _iso_utc(
        datetime.combine(
            utc_reference.date() - timedelta(days=1),
            clock_time.min,
            tzinfo=timezone.utc,
        )
    )
    override_targets = (
        uptrend_override_state.get("TARGETS", {})
        if isinstance(uptrend_override_state, Mapping)
        else {}
    )

    def override_matches(target: str) -> bool:
        decision = state["TARGETS"][target]
        return analysis_decision_matches_uptrend_override(
            target,
            decision,
            {
                "VERSION": UPTREND_OVERRIDE_STATE_VERSION,
                "TARGETS": override_targets,
            },
        )

    return (
        state.get("ANALYSIS_DATE") == analysis_date
        and state.get("POLICY_VERSION") == TIMING_POLICY_VERSION
        and all(
            state["TARGETS"][target].get("ANALYSIS_STATUS") == READY_STATUS
            and state["TARGETS"][target].get("RULES_HASH")
            == rules_hash(target, rules[target])
            and state["TARGETS"][target].get("ENABLED")
            is bool(rules[target]["BUY_ENABLED"])
            and state["TARGETS"][target]
            .get("SIGNALS", {})
            .get("DAILY_LAST_COMPLETE")
            == latest_completed_daily
            and override_matches(target)
            for target in selected_targets
        )
    )


def main() -> int:
    """Analyze selected targets, persist three decisions, and alert per asset."""

    generated = _utc_now()
    try:
        rules = validate_rules_map(DCA_TARGET_MAP_ENV)
        symbols = _parse_symbols(SYMBOLS_ENV, DCA_TARGET_MAP_ENV)
        uptrend_override_state = parse_uptrend_override_state(
            DCA_UPTREND_OVERRIDE_STATE_ENV
        )
        pre_analysis_uptrend_override_state = deepcopy(uptrend_override_state)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        send_to_discord(f"❌ **DCA analysis configuration error**\n{exc}", color=15_148_332)
        return 1

    selected_targets = [symbol.replace("/", "_") for symbol in symbols]
    state = _existing_or_empty_state(rules, generated, uptrend_override_state)
    analysis_date = generated.astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()
    already_complete = _analysis_is_complete_for_live_rules(
        state,
        rules,
        selected_targets,
        analysis_date,
        uptrend_override_state,
        now=generated,
    )
    if already_complete:
        try:
            live_override_state = _load_live_uptrend_override_state(
                operation="analysis no-op"
            )
        except Exception as exc:
            print(f"Analysis no-op recheck failed safely ({type(exc).__name__}).")
            send_to_discord(
                "❌ **DCA analysis no-op recheck failed**\n"
                "The live UPTREND override could not be verified; no cached "
                "decision was republished.",
                color=15_148_332,
            )
            return 1
        if live_override_state is None:
            already_complete = False
        elif live_override_state != uptrend_override_state:
            print("UPTREND override changed during analysis startup; retry required.")
            send_to_discord(
                "⚠️ **DCA UPTREND override changed during analysis**\n"
                "The stale snapshot was not used. Run analysis again to apply "
                "the current override state.",
                color=15_148_332,
            )
            return 1
    if already_complete:
        print(
            f"Analysis no-op: {analysis_date} already complete under "
            f"{TIMING_POLICY_VERSION}."
        )
        _write_actions_output(state)
        return 0

    state["VERSION"] = ANALYSIS_STATE_VERSION
    state["GENERATED_AT"] = _iso_utc(generated)
    state["POLICY_VERSION"] = TIMING_POLICY_VERSION
    state["ANALYSIS_DATE"] = analysis_date
    exchange = get_analysis_exchange()
    had_error = False
    override_released = False
    pending_reports: list[tuple[str, int]] = []

    for target in selected_targets:
        try:
            decision = analyze_asset(
                exchange,
                target,
                rules[target],
                now=generated,
                uptrend_override=uptrend_override_state["TARGETS"].get(target),
            )
            override_released = (
                _release_confirmed_uptrend_override(
                    uptrend_override_state,
                    target,
                    decision,
                    now=generated,
                )
                or override_released
            )
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
            decision = error_decision(
                target,
                rules[target],
                exc,
                now=generated,
                uptrend_override=uptrend_override_state["TARGETS"].get(target),
            )
            report = _decision_report(target, decision, rules[target])
            print(f"{target}: ERROR {exc}")
        state["TARGETS"][target] = decision
        pending_reports.append(
            (
                report,
                15_148_332
                if decision["ANALYSIS_STATUS"] != READY_STATUS
                else 3_447_003,
            )
        )

    try:
        # Structural validation happens before either persistence route. Every
        # analysis write is also bound to the override state it observed.
        validated = validate_analysis_state(state, rules)
        persisted = _persist_analysis_after_override_release(
            validated,
            uptrend_override_state,
            override_released=override_released,
            pre_analysis_uptrend_override_state=pre_analysis_uptrend_override_state,
        )
        _write_actions_output(validated)
    except Exception as exc:
        print(f"Analysis state commit failed safely ({type(exc).__name__}).")
        send_to_discord(
            "❌ **DCA analysis state commit failed**\n"
            "Computed decisions were not announced because their repository "
            "state could not be committed. Existing validated state remains "
            "authoritative.",
            color=15_148_332,
        )
        return 1

    for report, color in pending_reports:
        send_to_discord(report, color=color)
    print(
        f"Analysis complete: {len(selected_targets)} target(s), "
        f"state persistence={'direct' if persisted else 'workflow output'}; "
        f"errors={'yes' if had_error else 'no'}."
    )
    # Pair-local failures remain persisted, but the workflow must visibly fail
    # so the recovery schedules retry instead of treating partial analysis as healthy.
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import ANY, MagicMock, patch
from zoneinfo import ZoneInfo

import crypto_analysis
import dca_config


NOW = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)  # 04:00 Bangkok


def candle(timestamp, close):
    return [
        int(timestamp.timestamp() * 1000),
        close,
        close + 1,
        close - 1,
        close,
        100,
    ]


def trend_rows(direction="up", count=240):
    last_daily = datetime(2026, 8, 4, tzinfo=timezone.utc)
    daily = []
    for index in range(count):
        timestamp = last_daily - timedelta(days=count - 1 - index)
        close = 100 + index if direction == "up" else 500 - index
        daily.append(candle(timestamp, close))

    last_weekly = NOW - timedelta(days=8)
    weekly = []
    for index in range(30):
        timestamp = last_weekly - timedelta(weeks=29 - index)
        close = 100 + index if direction == "up" else 500 - index
        weekly.append(candle(timestamp, close))
    return daily, weekly


def intraday_rows(selected_time="03:00"):
    zone = ZoneInfo("Asia/Bangkok")
    local_now = NOW.astimezone(zone)
    rows = []
    for days_ago in range(60, 0, -1):
        local_date = local_now.date() - timedelta(days=days_ago)
        for quarter in range(96):
            hour, minute = divmod(quarter * 15, 60)
            local_ts = datetime(
                local_date.year,
                local_date.month,
                local_date.day,
                hour,
                minute,
                tzinfo=zone,
            )
            time_text = f"{hour:02d}:{minute:02d}"
            if time_text == selected_time:
                rows.append(candle(local_ts.astimezone(timezone.utc), 90.0))
            else:
                rows.append(candle(local_ts.astimezone(timezone.utc), 101.0))
    # Current local day keeps the 15m feed fresh but is intentionally excluded
    # from complete-day timing comparisons.
    for quarter in range(16):
        hour, minute = divmod(quarter * 15, 60)
        local_ts = datetime(
            local_now.year,
            local_now.month,
            local_now.day,
            hour,
            minute,
            tzinfo=zone,
        )
        rows.append(candle(local_ts.astimezone(timezone.utc), 101.0))
    # Starts exactly at analysis time, so it is incomplete and must be ignored.
    rows.append(candle(local_now.astimezone(timezone.utc), 1.0))
    return rows


def capped_rolling_intraday_rows(now, selected_time="03:00", count=720):
    """Model Kraken's latest-page cap at an arbitrary Bangkok time."""

    timeframe = timedelta(minutes=15)
    epoch_seconds = int(now.timestamp())
    latest_complete_end = datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % int(timeframe.total_seconds())),
        tz=timezone.utc,
    )
    latest_start = latest_complete_end - timeframe
    first_start = latest_start - timeframe * (count - 1)
    zone = ZoneInfo("Asia/Bangkok")
    rows = []
    for index in range(count):
        timestamp = first_start + timeframe * index
        local_time = timestamp.astimezone(zone).strftime("%H:%M")
        rows.append(candle(timestamp, 90.0 if local_time == selected_time else 101.0))
    return rows


def candidate(time_text, miss, win, days):
    return {"TIME": time_text, "MEDIAN_MISS": miss, "WIN_RATE": win, "DAYS": days}


def ready_history():
    return {
        "VERSION": 1, "STATUS": "READY", "PAIR": "BTC/GBP",
        "FROM": "2026-06-01T00:00:00Z", "THROUGH": "2026-08-05T21:00:00Z",
        "CANDLE_COUNT": 6240, "NO_TRADE_INTERVALS": 0,
        "OVERLAP": {"STATUS": "MATCH"}, "HASH": "a" * 64,
    }


def uptrend_override_state(
    target="BTC_GBP",
    *,
    active=True,
    activated_at="2026-08-01T00:00:00Z",
    released_at=None,
    reason="Unprecedented market transition",
):
    if not active and released_at is None:
        released_at = "2026-08-02T00:00:00Z"
    return {
        "VERSION": 1,
        "TARGETS": {
            target: {
                "ACTIVE": active,
                "ACTIVATED_AT": activated_at,
                "RELEASED_AT": released_at,
                "REASON": reason,
            }
        },
    }


class AnalysisSymbolTests(unittest.TestCase):
    def test_analysis_exchange_is_kraken_only(self):
        with self.assertRaisesRegex(ValueError, "configured Kraken markets only"):
            crypto_analysis.get_analysis_exchange("coinbase")

    def test_derives_exact_three_kraken_gbp_pairs_from_rules(self):
        rules = dca_config.default_rules_map()
        self.assertEqual(
            crypto_analysis._parse_symbols("", json.dumps(rules)),
            ["BTC/GBP", "ETH/GBP", "SOL/GBP"],
        )
        self.assertEqual(
            crypto_analysis._parse_symbols("all", json.dumps(rules)),
            ["BTC/GBP", "ETH/GBP", "SOL/GBP"],
        )

    def test_explicit_supported_subset_is_normalized(self):
        self.assertEqual(
            crypto_analysis._parse_symbols('BTC,ETH_GBP,"SOL/GBP"', "{}"),
            ["BTC/GBP", "ETH/GBP", "SOL/GBP"],
        )

    def test_nonproduction_and_legacy_quote_pairs_are_rejected(self):
        for symbol in ("BTC/USD", "ETH/USD", "LINK/USD", "BTC_THB"):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(ValueError, "Only BTC/GBP"):
                    crypto_analysis._parse_symbols(symbol, "{}")

    def test_rules_source_rejects_legacy_schema(self):
        with self.assertRaisesRegex(ValueError, "missing production targets"):
            crypto_analysis._parse_symbols(
                "", '{"BTC_GBP":{"TIME":"02:45","AMOUNT_GBP":10}}'
            )


class TrendClassificationTests(unittest.TestCase):
    def test_ten_consecutive_closes_confirm_uptrend_and_old_downtrend_is_preserved(self):
        daily, weekly = trend_rows("up")
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")
        self.assertEqual(signals["UPTREND_CONFIRMATION_REQUIRED"], 10)
        self.assertEqual(signals["UPTREND_CONFIRMATION_COUNT"], 10)
        self.assertTrue(signals["UPTREND_CONFIRMED"])
        self.assertTrue(signals["TWO_DAY_ABOVE"])
        self.assertTrue(signals["WEEKLY_ABOVE"])
        self.assertTrue(signals["SLOPE_POSITIVE"])

        daily, weekly = trend_rows("down")
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "DOWNTREND")
        self.assertTrue(signals["TWO_DAY_BELOW"])
        self.assertTrue(signals["WEEKLY_BELOW"])
        self.assertTrue(signals["SLOPE_NEGATIVE"])
        self.assertFalse(signals["UPTREND_CONFIRMED"])

    def test_nine_consecutive_closes_are_sideways_even_when_old_up_signals_pass(self):
        daily, weekly = trend_rows("up")
        daily[-10][1:5] = [1, 2, 0.5, 1]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "SIDEWAYS")
        self.assertEqual(signals["UPTREND_CONFIRMATION_COUNT"], 9)
        self.assertFalse(signals["UPTREND_CONFIRMED"])
        self.assertTrue(signals["TWO_DAY_ABOVE"])
        self.assertTrue(signals["WEEKLY_ABOVE"])
        self.assertTrue(signals["SLOPE_POSITIVE"])

    def test_break_before_the_ten_candle_window_does_not_block_uptrend(self):
        daily, weekly = trend_rows("up")
        daily[-11][1:5] = [1, 2, 0.5, 1]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")
        self.assertEqual(signals["UPTREND_CONFIRMATION_COUNT"], 10)

    def test_each_close_is_compared_with_its_own_sma150(self):
        daily, weekly = trend_rows("up", count=170)
        for row in daily:
            row[1:5] = [100, 101, 99, 100]
        for index, close in enumerate([101, *([1000] * 9)], start=160):
            daily[index][1:5] = [close, close + 1, close - 1, close]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")
        self.assertTrue(signals["UPTREND_CONFIRMED"])
        self.assertLess(101, signals["DAILY_SMA150"])

    def test_uptrend_no_longer_depends_on_weekly_confirmation(self):
        daily, weekly = trend_rows("up")
        for row in weekly:
            row[1:5] = [100, 101, 99, 100]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")
        self.assertTrue(signals["UPTREND_CONFIRMED"])
        self.assertFalse(signals["WEEKLY_ABOVE"])
        self.assertFalse(signals["WEEKLY_BELOW"])

    def test_deployed_downtrend_still_requires_weekly_ema_and_slope_confirmation(self):
        daily, weekly = trend_rows("down")
        for row in weekly:
            row[1:5] = [100, 101, 99, 100]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "SIDEWAYS")
        self.assertTrue(signals["TWO_DAY_BELOW"])
        self.assertFalse(signals["WEEKLY_BELOW"])
        self.assertTrue(signals["SLOPE_NEGATIVE"])

    def test_all_equal_and_zero_slope_are_strictly_sideways(self):
        daily, weekly = trend_rows("up")
        for row in [*daily, *weekly]:
            row[1:5] = [100, 101, 99, 100]
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "SIDEWAYS")
        self.assertFalse(signals["TWO_DAY_ABOVE"])
        self.assertFalse(signals["TWO_DAY_BELOW"])
        self.assertEqual(signals["SMA150_SLOPE_20D"], 0)
        self.assertFalse(signals["SLOPE_POSITIVE"])
        self.assertFalse(signals["SLOPE_NEGATIVE"])
        self.assertEqual(signals["UPTREND_CONFIRMATION_COUNT"], 0)
        self.assertFalse(signals["UPTREND_CONFIRMED"])

    def test_latest_daily_and_weekly_gaps_fail_closed(self):
        daily, weekly = trend_rows("up")
        daily.pop(-2)
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "candle gap"):
            crypto_analysis.classify_trend(daily, weekly, now=NOW)

        daily, weekly = trend_rows("up")
        weekly.pop(-2)
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "candle gap"):
            crypto_analysis.classify_trend(daily, weekly, now=NOW)

    def test_missing_latest_daily_or_weekly_candle_is_stale(self):
        daily, weekly = trend_rows("up")
        daily.pop()
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "stale"):
            crypto_analysis.classify_trend(daily, weekly, now=NOW)

        daily, weekly = trend_rows("up")
        weekly.pop()
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "stale"):
            crypto_analysis.classify_trend(daily, weekly, now=NOW)

    def test_incomplete_daily_and_weekly_candles_are_ignored(self):
        daily, weekly = trend_rows("up")
        daily.append(candle(datetime(2026, 8, 5, tzinfo=timezone.utc), 1))
        weekly.append(candle(NOW - timedelta(days=1), 1))
        regime, signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")
        self.assertNotEqual(signals["DAILY_CLOSE"], 1)
        self.assertNotEqual(signals["WEEKLY_CLOSE"], 1)
        self.assertTrue(signals["UPTREND_CONFIRMED"])

    def test_insufficient_and_stale_market_data_fail_closed(self):
        daily, weekly = trend_rows("up", count=169)
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "insufficient"):
            crypto_analysis.classify_trend(daily, weekly, now=NOW)

        daily, weekly = trend_rows("up", count=170)
        regime, _signals = crypto_analysis.classify_trend(daily, weekly, now=NOW)
        self.assertEqual(regime, "UPTREND")

        daily, weekly = trend_rows("up")
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "stale"):
            crypto_analysis.classify_trend(
                daily, weekly, now=NOW + timedelta(days=4)
            )


class UptrendOverrideTests(unittest.TestCase):
    def test_optional_override_is_empty_and_valid_state_is_normalized(self):
        self.assertEqual(
            crypto_analysis.parse_uptrend_override_state(""),
            {"VERSION": 1, "TARGETS": {}},
        )
        expected = uptrend_override_state()
        self.assertEqual(
            crypto_analysis.parse_uptrend_override_state(json.dumps(expected)),
            expected,
        )

    def test_override_schema_rejects_unknown_or_inconsistent_state(self):
        valid = uptrend_override_state()
        invalid_states = {
            "missing fields": {},
            "wrong version": {**valid, "VERSION": 2},
            "boolean version": {**valid, "VERSION": True},
            "unknown target": {
                "VERSION": 1,
                "TARGETS": {"DOGE_GBP": next(iter(valid["TARGETS"].values()))},
            },
            "extra entry field": {
                "VERSION": 1,
                "TARGETS": {
                    "BTC_GBP": {
                        **valid["TARGETS"]["BTC_GBP"],
                        "REGIME": "UPTREND",
                    }
                },
            },
            "active with release": uptrend_override_state(
                released_at="2026-08-02T00:00:00Z"
            ),
            "inactive without release": {
                "VERSION": 1,
                "TARGETS": {
                    "BTC_GBP": {
                        **valid["TARGETS"]["BTC_GBP"],
                        "ACTIVE": False,
                    }
                },
            },
            "noncanonical timestamp": uptrend_override_state(
                activated_at="2026-08-01T00:00:00+00:00"
            ),
            "empty reason": uptrend_override_state(reason=""),
        }
        for label, state in invalid_states.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                crypto_analysis.parse_uptrend_override_state(state)

    def test_active_override_forces_uptrend_without_hiding_normal_regime(self):
        entry = uptrend_override_state()["TARGETS"]["BTC_GBP"]
        regime, signals = crypto_analysis._apply_uptrend_override(
            "DOWNTREND",
            {"UPTREND_CONFIRMED": False},
            entry,
        )
        self.assertEqual(regime, "UPTREND")
        self.assertEqual(signals["REGIME_WITHOUT_OVERRIDE"], "DOWNTREND")
        self.assertTrue(signals["UPTREND_OVERRIDE_ACTIVE"])
        self.assertTrue(signals["UPTREND_OVERRIDE_APPLIED"])
        self.assertEqual(
            signals["UPTREND_OVERRIDE_REASON"], "Unprecedented market transition"
        )
        self.assertFalse(signals["UPTREND_OVERRIDE_AUTO_RELEASED"])

    def test_confirmed_normal_uptrend_auto_releases_override(self):
        state = uptrend_override_state()
        entry = state["TARGETS"]["BTC_GBP"]
        regime, signals = crypto_analysis._apply_uptrend_override(
            "UPTREND",
            {"UPTREND_CONFIRMED": True},
            entry,
        )
        decision = {"REGIME": regime, "SIGNALS": signals}

        self.assertTrue(
            crypto_analysis._release_confirmed_uptrend_override(
                state, "BTC_GBP", decision, now=NOW
            )
        )
        released = state["TARGETS"]["BTC_GBP"]
        self.assertFalse(released["ACTIVE"])
        self.assertEqual(released["RELEASED_AT"], "2026-08-05T21:00:00Z")
        self.assertFalse(decision["SIGNALS"]["UPTREND_OVERRIDE_ACTIVE"])
        self.assertFalse(decision["SIGNALS"]["UPTREND_OVERRIDE_APPLIED"])
        self.assertTrue(decision["SIGNALS"]["UPTREND_OVERRIDE_AUTO_RELEASED"])

    def test_unconfirmed_override_remains_active(self):
        state = uptrend_override_state()
        entry = state["TARGETS"]["BTC_GBP"]
        regime, signals = crypto_analysis._apply_uptrend_override(
            "SIDEWAYS",
            {"UPTREND_CONFIRMED": False},
            entry,
        )
        decision = {"REGIME": regime, "SIGNALS": signals}
        self.assertFalse(
            crypto_analysis._release_confirmed_uptrend_override(
                state, "BTC_GBP", decision, now=NOW
            )
        )
        self.assertTrue(state["TARGETS"]["BTC_GBP"]["ACTIVE"])
        self.assertIsNone(state["TARGETS"]["BTC_GBP"]["RELEASED_AT"])

    def test_error_and_history_not_ready_decisions_preserve_active_override_audit(self):
        rule = dca_config.default_rules_map()["BTC_GBP"]
        entry = uptrend_override_state()["TARGETS"]["BTC_GBP"]
        for error, expected_status in (
            ("market data failed", "ERROR"),
            (crypto_analysis.HistoryError("history is incomplete"), "HISTORY_NOT_READY"),
        ):
            with self.subTest(status=expected_status):
                decision = crypto_analysis.error_decision(
                    "BTC_GBP",
                    rule,
                    error,
                    now=NOW,
                    uptrend_override=entry,
                )

                self.assertEqual(decision["ANALYSIS_STATUS"], expected_status)
                self.assertIsNone(decision["REGIME"])
                self.assertTrue(decision["SIGNALS"]["UPTREND_OVERRIDE_ACTIVE"])
                self.assertFalse(decision["SIGNALS"]["UPTREND_OVERRIDE_APPLIED"])
                self.assertEqual(
                    decision["SIGNALS"]["UPTREND_OVERRIDE_REASON"],
                    entry["REASON"],
                )
                report = crypto_analysis._decision_report(
                    "BTC_GBP", decision, rule
                )
                self.assertIn("Emergency override: `ACTIVE`", report)
                self.assertIn("orders remain blocked", report)


class TimingPolicyTests(unittest.TestCase):
    @staticmethod
    def all_window_tables(overrides):
        tables = {
            days: [candidate(f"{days % 24:02d}:00", 9, 0, days)]
            for days in crypto_analysis.PERIODS
        }
        tables.update(overrides)
        return tables

    def test_completed_15m_data_selects_expected_time_and_ignores_current_candle(self):
        selected, timing = crypto_analysis.select_best_time(
            intraday_rows("03:00"), now=NOW, local_tz="Asia/Bangkok"
        )
        self.assertEqual(selected, "03:00")
        self.assertEqual(timing["TIMEZONE"], "Asia/Bangkok")
        self.assertEqual(timing["WINDOWS"]["60"]["BEST"]["TIME"], "03:00")
        self.assertEqual(
            set(timing["WINDOWS"]), {"3", "5", "7", "14", "30", "45", "60"}
        )
        self.assertEqual(timing["HISTORY_CANDLES"], 5760)

    def test_kraken_ohlc_cap_cannot_substitute_for_strict_history(self):
        late_now = datetime(2026, 8, 5, 16, 52, tzinfo=timezone.utc)  # 23:52 Bangkok
        rows = capped_rolling_intraday_rows(late_now, "03:00")
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "need 60"):
            crypto_analysis.select_best_time(rows, now=late_now, local_tz="Asia/Bangkok")

    def test_intraday_shortage_and_any_gap_fail_closed(self):
        rows = intraday_rows("03:00")
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "need 60"):
            crypto_analysis.select_best_time(rows[96:], now=NOW)
        zone = ZoneInfo("Asia/Bangkok")
        missing_day = NOW.astimezone(zone).date() - timedelta(days=30)
        rows = [row for row in rows if not (
            datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).astimezone(zone).date() == missing_day
            and datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).astimezone(zone).strftime("%H:%M") == "03:00"
        )]
        selected, _ = crypto_analysis.select_best_time(rows, now=NOW)
        self.assertNotEqual(selected, "03:00")

    def test_kraken_fetch_is_one_latest_page_without_false_pagination(self):
        exchange = MagicMock()
        expected = capped_rolling_intraday_rows(NOW)
        exchange.fetch_ohlcv.return_value = expected
        actual = crypto_analysis.fetch_ohlcv_last_n_days(
            exchange, "BTC/GBP", "15m", 7
        )
        self.assertEqual(actual, expected)
        exchange.fetch_ohlcv.assert_called_once_with(
            "BTC/GBP", timeframe="15m", limit=720
        )

    def test_fourteen_day_override_thresholds_are_inclusive(self):
        tables = self.all_window_tables({
            14: [candidate("03:00", 0.70, 60, 14)],
            30: [candidate("05:00", 0.50, 50, 30)],
            45: [candidate("06:00", 0.55, 45, 45)],
            60: [candidate("07:00", 0.50, 50, 60)],
        })
        selected, window, rule, _ = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual((selected["TIME"], window, rule), ("03:00", 14, "RECENCY_14D_OVERRIDE"))

        tables[14][0]["WIN_RATE"] = 59.999
        selected, window, rule, _ = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual((selected["TIME"], window, rule), ("07:00", 60, "BASE_60D"))

    def test_thirty_day_requires_material_median_improvement(self):
        tables = self.all_window_tables({
            14: [candidate("03:00", 1.0, 0, 14)],
            30: [candidate("05:00", 0.35, 40, 30)],
            45: [candidate("06:00", 0.45, 45, 45)],
            60: [candidate("07:00", 0.50, 50, 60)],
        })
        selected, window, rule, _ = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual(
            (selected["TIME"], window, rule),
            ("05:00", 30, "BASE_30D_MATERIAL_IMPROVEMENT"),
        )

        tables[30][0]["MEDIAN_MISS"] = 0.351
        selected, window, rule, _ = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual((selected["TIME"], window, rule), ("07:00", 60, "BASE_60D"))

    def test_near_tie_prefers_cross_window_presence_then_win_rate_then_earlier(self):
        tables = self.all_window_tables({
            3: [candidate("08:00", 2, 2, 3)],
            5: [candidate("08:00", 2, 2, 5)],
            7: [candidate("08:00", 2, 2, 7)],
            14: [candidate("10:00", 5, 1, 14)],
            30: [candidate("08:00", 2, 2, 30), candidate("09:00", 3, 1, 30)],
            45: [candidate("08:00", 2, 2, 45), candidate("10:00", 3, 1, 45)],
            60: [candidate("09:00", 0.10, 90, 60), candidate("08:00", 0.19, 80, 60)],
        })
        selected, window, _, appearances = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual(window, 60)
        self.assertEqual(selected["TIME"], "08:00")
        self.assertEqual(appearances, 6)

        # Equal appearances and 7d win rate fall through to earlier HH:MM.
        tables = self.all_window_tables({
            14: [candidate("10:00", 5, 1, 14)],
            30: [candidate("09:00", 1, 1, 30), candidate("08:00", 2, 1, 30)],
            45: [candidate("09:00", 1, 1, 45), candidate("08:00", 2, 1, 45)],
            60: [candidate("09:00", 0.10, 80, 60), candidate("08:00", 0.19, 80, 60)],
        })
        selected, _, _, _ = crypto_analysis.choose_timing_candidate(tables)
        self.assertEqual(selected["TIME"], "08:00")

    def test_next_occurrence_honours_bangkok_timezone_and_30_minute_notice(self):
        self.assertEqual(
            crypto_analysis.next_execution_time(
                "04:30", analyzed_at=NOW, local_tz="Asia/Bangkok"
            ),
            datetime(2026, 8, 5, 21, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            crypto_analysis.next_execution_time(
                "04:29", analyzed_at=NOW, local_tz="Asia/Bangkok"
            ),
            datetime(2026, 8, 5, 21, 29, tzinfo=timezone.utc),
        )

    def test_missing_full_days_fail_closed(self):
        rows = intraday_rows()[96:]
        with self.assertRaisesRegex(crypto_analysis.AnalysisError, "need 60"):
            crypto_analysis.select_best_time(rows, now=NOW)


class DecisionAndNarrationTests(unittest.TestCase):
    @staticmethod
    def ready_report_decision():
        return {
            "ENABLED": False,
            "ANALYSIS_STATUS": "READY",
            "EXECUTION_STATUS": "DISABLED",
            "REGIME": "UPTREND",
            "AMOUNT_TIER": "LOW",
            "SELECTED_AT": "2026-08-05T22:00:00Z",
            "EXECUTE_AT": "2026-08-05T22:00:00Z",
            "VALID_UNTIL": "2026-08-05T23:00:00Z",
            "CATCHUP_APPLIED": False,
            "DECISION_ID": "btc-ready-report",
            "RULES_HASH": "a" * 64,
            "POLICY_VERSION": dca_config.TIMING_POLICY_VERSION,
            "ANALYSIS_DATE": "2026-08-06",
            "HISTORY": {
                "STATUS": "READY",
                "FROM": "2026-06-01T00:00:00Z",
                "THROUGH": "2026-08-05T21:00:00Z",
                "HASH": "b" * 64,
            },
            "SIGNALS": {"UPTREND_CONFIRMED": False},
            "TIMING": {
                "SELECTED_LOCAL_TIME": "05:00",
                "TIMEZONE": "Asia/Bangkok",
                "SELECTION_RULE": "test",
            },
            "ERROR": None,
        }

    def test_target_migration_lock_blocks_direct_analysis_persistence(self):
        locked = MagicMock(status_code=200)
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(crypto_analysis.requests, "get", return_value=locked),
            patch.object(crypto_analysis.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "migration lock"),
        ):
            crypto_analysis.persist_analysis_state({"safe": "state"})
        write.assert_not_called()

    def test_uncertain_target_migration_lock_state_blocks_persistence(self):
        uncertain = MagicMock(status_code=503)
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(crypto_analysis.requests, "get", return_value=uncertain),
            patch.object(crypto_analysis.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "could not be checked"),
        ):
            crypto_analysis.persist_analysis_state({"safe": "state"})
        write.assert_not_called()

    def test_target_migration_lock_blocks_override_persistence(self):
        locked = MagicMock(status_code=200)
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(crypto_analysis.requests, "get", return_value=locked),
            patch.object(crypto_analysis.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "migration lock"),
        ):
            crypto_analysis.persist_uptrend_override_state(
                uptrend_override_state(active=False),
                expected_state=uptrend_override_state(),
            )
        write.assert_not_called()

    def test_override_release_rechecks_live_state_immediately_before_write(self):
        before = uptrend_override_state()
        after = uptrend_override_state(active=False)
        unlocked = MagicMock(status_code=404)
        current = MagicMock(status_code=200)
        current.json.return_value = {"value": json.dumps(before)}
        written = MagicMock(status_code=204)
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(
                crypto_analysis.requests,
                "get",
                side_effect=[unlocked, current],
            ) as reads,
            patch.object(
                crypto_analysis.requests,
                "patch",
                return_value=written,
            ) as write,
        ):
            self.assertTrue(
                crypto_analysis.persist_uptrend_override_state(
                    after,
                    expected_state=before,
                )
            )

        self.assertEqual(reads.call_count, 2)
        self.assertTrue(
            reads.call_args_list[1].args[0].endswith(
                "/actions/variables/DCA_UPTREND_OVERRIDE_STATE"
            )
        )
        write.assert_called_once()

    def test_override_release_aborts_if_live_state_changed_during_analysis(self):
        expected = uptrend_override_state(reason="Initial reason")
        changed = uptrend_override_state(reason="Operator changed this")
        released = uptrend_override_state(
            active=False,
            reason="Initial reason",
        )
        unlocked = MagicMock(status_code=404)
        current = MagicMock(status_code=200)
        current.json.return_value = {"value": json.dumps(changed)}
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(
                crypto_analysis.requests,
                "get",
                side_effect=[unlocked, current],
            ),
            patch.object(crypto_analysis.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "changed during analysis"),
        ):
            crypto_analysis.persist_uptrend_override_state(
                released,
                expected_state=expected,
            )
        write.assert_not_called()

    def test_override_release_is_persisted_before_analysis_state(self):
        order = []
        before = uptrend_override_state()
        with (
            patch.object(
                crypto_analysis,
                "persist_uptrend_override_state",
                side_effect=lambda _state, *, expected_state: (
                    order.append("override")
                    or self.assertEqual(expected_state, before)
                    or True
                ),
            ),
            patch.object(
                crypto_analysis,
                "persist_analysis_state",
                side_effect=lambda _state, *, expected_uptrend_override_state: (
                    order.append("analysis")
                    or self.assertEqual(
                        expected_uptrend_override_state,
                        uptrend_override_state(active=False),
                    )
                    or True
                ),
            ),
        ):
            self.assertTrue(
                crypto_analysis._persist_analysis_after_override_release(
                    {"analysis": "state"},
                    uptrend_override_state(active=False),
                    override_released=True,
                    pre_analysis_uptrend_override_state=before,
                )
            )
        self.assertEqual(order, ["override", "analysis"])

    def test_analysis_write_rechecks_unchanged_override_without_a_release(self):
        expected = uptrend_override_state()
        unlocked = MagicMock(status_code=404)
        current = MagicMock(status_code=200)
        current.json.return_value = {"value": json.dumps(expected)}
        written = MagicMock(status_code=204)
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(
                crypto_analysis.requests,
                "get",
                side_effect=[unlocked, current],
            ) as reads,
            patch.object(
                crypto_analysis.requests,
                "patch",
                return_value=written,
            ) as write,
        ):
            self.assertTrue(
                crypto_analysis.persist_analysis_state(
                    {"analysis": "state"},
                    expected_uptrend_override_state=expected,
                )
            )

        self.assertEqual(reads.call_count, 2)
        self.assertTrue(
            reads.call_args_list[1].args[0].endswith(
                "/actions/variables/DCA_UPTREND_OVERRIDE_STATE"
            )
        )
        write.assert_called_once()

    def test_analysis_write_aborts_if_override_changes_during_analysis(self):
        expected = uptrend_override_state(reason="Initial reason")
        changed = uptrend_override_state(reason="Operator changed this")
        unlocked = MagicMock(status_code=404)
        current = MagicMock(status_code=200)
        current.json.return_value = {"value": json.dumps(changed)}
        with (
            patch.dict(
                crypto_analysis.os.environ,
                {"GH_PAT_FOR_VARS": "token", "GITHUB_REPOSITORY": "owner/repo"},
            ),
            patch.object(
                crypto_analysis.requests,
                "get",
                side_effect=[unlocked, current],
            ),
            patch.object(crypto_analysis.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "state write aborted"),
        ):
            crypto_analysis.persist_analysis_state(
                {"analysis": "state"},
                expected_uptrend_override_state=expected,
            )
        write.assert_not_called()

    def test_missing_optional_override_matches_expected_empty_state(self):
        for label, live_response in (
            ("missing", MagicMock(status_code=404)),
            ("present blank", MagicMock(status_code=200)),
        ):
            if live_response.status_code == 200:
                live_response.json.return_value = {"value": ""}
            unlocked = MagicMock(status_code=404)
            written = MagicMock(status_code=204)
            with (
                self.subTest(label=label),
                patch.dict(
                    crypto_analysis.os.environ,
                    {
                        "GH_PAT_FOR_VARS": "token",
                        "GITHUB_REPOSITORY": "owner/repo",
                    },
                ),
                patch.object(
                    crypto_analysis.requests,
                    "get",
                    side_effect=[unlocked, live_response],
                ),
                patch.object(
                    crypto_analysis.requests,
                    "patch",
                    return_value=written,
                ) as write,
            ):
                self.assertTrue(
                    crypto_analysis.persist_analysis_state(
                        {"analysis": "state"},
                        expected_uptrend_override_state={
                            "VERSION": 1,
                            "TARGETS": {},
                        },
                    )
                )
            write.assert_called_once()

    def test_analysis_persistence_is_blocked_if_release_cannot_be_written(self):
        with (
            patch.object(
                crypto_analysis,
                "persist_uptrend_override_state",
                return_value=False,
            ),
            patch.object(crypto_analysis, "persist_analysis_state") as analysis_write,
            self.assertRaisesRegex(RuntimeError, "requires direct repository persistence"),
        ):
            crypto_analysis._persist_analysis_after_override_release(
                {"analysis": "state"},
                uptrend_override_state(active=False),
                override_released=True,
                pre_analysis_uptrend_override_state=uptrend_override_state(),
            )
        analysis_write.assert_not_called()

    def test_main_announces_decision_only_after_state_commit(self):
        order = []
        rules = dca_config.default_rules_map()
        with (
            patch.object(crypto_analysis, "_utc_now", return_value=NOW),
            patch.object(crypto_analysis, "SYMBOLS_ENV", "BTC"),
            patch.object(
                crypto_analysis,
                "DCA_TARGET_MAP_ENV",
                json.dumps(rules),
            ),
            patch.object(crypto_analysis, "DCA_ANALYSIS_STATE_ENV", ""),
            patch.object(crypto_analysis, "DCA_UPTREND_OVERRIDE_STATE_ENV", ""),
            patch.object(crypto_analysis, "get_analysis_exchange", return_value=MagicMock()),
            patch.object(
                crypto_analysis,
                "analyze_asset",
                return_value=self.ready_report_decision(),
            ),
            patch.object(
                crypto_analysis,
                "validate_analysis_state",
                side_effect=lambda state, _rules: state,
            ),
            patch.object(
                crypto_analysis,
                "_persist_analysis_after_override_release",
                side_effect=lambda *_args, **_kwargs: order.append("commit") or True,
            ),
            patch.object(crypto_analysis, "_write_actions_output"),
            patch.object(
                crypto_analysis,
                "get_ai_summary",
                return_value=("unused", None, None),
            ),
            patch.object(
                crypto_analysis,
                "send_to_discord",
                side_effect=lambda *_args, **_kwargs: order.append("announce") or True,
            ),
        ):
            self.assertEqual(crypto_analysis.main(), 0)

        self.assertEqual(order, ["commit", "announce"])

    def test_main_sends_only_failure_alert_when_state_commit_fails(self):
        rules = dca_config.default_rules_map()
        with (
            patch.object(crypto_analysis, "_utc_now", return_value=NOW),
            patch.object(crypto_analysis, "SYMBOLS_ENV", "BTC"),
            patch.object(
                crypto_analysis,
                "DCA_TARGET_MAP_ENV",
                json.dumps(rules),
            ),
            patch.object(crypto_analysis, "DCA_ANALYSIS_STATE_ENV", ""),
            patch.object(crypto_analysis, "DCA_UPTREND_OVERRIDE_STATE_ENV", ""),
            patch.object(crypto_analysis, "get_analysis_exchange", return_value=MagicMock()),
            patch.object(
                crypto_analysis,
                "analyze_asset",
                return_value=self.ready_report_decision(),
            ),
            patch.object(
                crypto_analysis,
                "validate_analysis_state",
                side_effect=lambda state, _rules: state,
            ),
            patch.object(
                crypto_analysis,
                "_persist_analysis_after_override_release",
                side_effect=RuntimeError("write failed"),
            ),
            patch.object(crypto_analysis, "_write_actions_output") as output,
            patch.object(
                crypto_analysis,
                "get_ai_summary",
                return_value=("unused", None, None),
            ),
            patch.object(crypto_analysis, "send_to_discord", return_value=True) as notify,
        ):
            self.assertEqual(crypto_analysis.main(), 1)

        output.assert_not_called()
        notify.assert_called_once()
        self.assertIn("state commit failed", notify.call_args.args[0])
        self.assertNotIn("daily decision", notify.call_args.args[0])

    def test_main_noop_aborts_if_live_override_changed_after_workflow_load(self):
        rules = dca_config.default_rules_map()
        changed = uptrend_override_state()
        with (
            patch.object(crypto_analysis, "_utc_now", return_value=NOW),
            patch.object(crypto_analysis, "SYMBOLS_ENV", "BTC"),
            patch.object(
                crypto_analysis,
                "DCA_TARGET_MAP_ENV",
                json.dumps(rules),
            ),
            patch.object(crypto_analysis, "DCA_ANALYSIS_STATE_ENV", ""),
            patch.object(crypto_analysis, "DCA_UPTREND_OVERRIDE_STATE_ENV", ""),
            patch.object(
                crypto_analysis,
                "_analysis_is_complete_for_live_rules",
                return_value=True,
            ),
            patch.object(
                crypto_analysis,
                "_load_live_uptrend_override_state",
                return_value=changed,
            ),
            patch.object(crypto_analysis, "get_analysis_exchange") as exchange,
            patch.object(crypto_analysis, "_write_actions_output") as output,
            patch.object(crypto_analysis, "send_to_discord", return_value=True) as notify,
        ):
            self.assertEqual(crypto_analysis.main(), 1)

        exchange.assert_not_called()
        output.assert_not_called()
        notify.assert_called_once()
        self.assertIn("override changed", notify.call_args.args[0])

    def test_analysis_noop_is_invalidated_when_enable_state_changes(self):
        rules = dca_config.default_rules_map()
        target = "BTC_GBP"
        rules[target]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = dca_config.empty_analysis_state(rules, now=NOW)
        state["ANALYSIS_DATE"] = NOW.astimezone(
            ZoneInfo("Asia/Bangkok")
        ).date().isoformat()
        state["POLICY_VERSION"] = dca_config.TIMING_POLICY_VERSION
        decision = state["TARGETS"][target]
        decision["ANALYSIS_STATUS"] = "READY"
        decision["RULES_HASH"] = dca_config.rules_hash(target, rules[target])
        decision["ENABLED"] = False
        _, decision["SIGNALS"] = crypto_analysis._apply_uptrend_override(
            "SIDEWAYS",
            {"DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z"},
            None,
        )
        self.assertTrue(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state, rules, [target], state["ANALYSIS_DATE"], now=NOW
            )
        )
        rules[target]["BUY_ENABLED"] = True
        self.assertFalse(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state, rules, [target], state["ANALYSIS_DATE"], now=NOW
            )
        )

    def test_analysis_noop_is_invalidated_by_newly_completed_daily_candle(self):
        rules = dca_config.default_rules_map()
        target = "BTC_GBP"
        before_midnight = datetime(2026, 8, 5, 23, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 8, 6, 0, 1, tzinfo=timezone.utc)
        analysis_date = before_midnight.astimezone(
            ZoneInfo("Asia/Bangkok")
        ).date().isoformat()
        self.assertEqual(
            analysis_date,
            after_midnight.astimezone(ZoneInfo("Asia/Bangkok")).date().isoformat(),
        )
        state = dca_config.empty_analysis_state(rules, now=before_midnight)
        state["ANALYSIS_DATE"] = analysis_date
        decision = state["TARGETS"][target]
        decision["ANALYSIS_STATUS"] = "READY"
        _, decision["SIGNALS"] = crypto_analysis._apply_uptrend_override(
            "SIDEWAYS",
            {"DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z"},
            None,
        )

        self.assertTrue(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state,
                rules,
                [target],
                analysis_date,
                now=before_midnight,
            )
        )
        self.assertFalse(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state,
                rules,
                [target],
                analysis_date,
                now=after_midnight,
            )
        )

    def test_analysis_noop_requires_decision_to_match_active_override(self):
        rules = dca_config.default_rules_map()
        target = "BTC_GBP"
        state = dca_config.empty_analysis_state(rules, now=NOW)
        state["ANALYSIS_DATE"] = NOW.astimezone(
            ZoneInfo("Asia/Bangkok")
        ).date().isoformat()
        decision = state["TARGETS"][target]
        decision["ANALYSIS_STATUS"] = "READY"
        override = uptrend_override_state()

        self.assertFalse(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state, rules, [target], state["ANALYSIS_DATE"], override
            )
        )

        entry = override["TARGETS"][target]
        regime, signals = crypto_analysis._apply_uptrend_override(
            "SIDEWAYS",
            {
                "UPTREND_CONFIRMED": False,
                "DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z",
            },
            entry,
        )
        decision["REGIME"] = regime
        decision["SIGNALS"] = signals
        self.assertTrue(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state,
                rules,
                [target],
                state["ANALYSIS_DATE"],
                override,
                now=NOW,
            )
        )

        inactive = uptrend_override_state(active=False)
        normal_regime, signals = crypto_analysis._apply_uptrend_override(
            "SIDEWAYS",
            {"DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z"},
            inactive["TARGETS"][target],
        )
        decision["REGIME"] = normal_regime
        decision["SIGNALS"] = signals
        self.assertTrue(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state,
                rules,
                [target],
                state["ANALYSIS_DATE"],
                inactive,
                now=NOW,
            )
        )
        self.assertFalse(
            crypto_analysis._analysis_is_complete_for_live_rules(
                state,
                rules,
                [target],
                state["ANALYSIS_DATE"],
                {"VERSION": 1, "TARGETS": {}},
                now=NOW,
            )
        )

    def test_v1_state_is_replaced_with_fail_closed_v2_before_fresh_analysis(self):
        rules = dca_config.default_rules_map()
        old_state = dca_config.empty_analysis_state(rules, now=NOW)
        old_state["VERSION"] = 1
        with patch.object(
            crypto_analysis,
            "DCA_ANALYSIS_STATE_ENV",
            json.dumps(old_state),
        ):
            replacement = crypto_analysis._existing_or_empty_state(rules, NOW)

        self.assertEqual(replacement["VERSION"], dca_config.ANALYSIS_STATE_VERSION)
        self.assertTrue(
            all(
                decision["ANALYSIS_STATUS"] != "READY"
                for decision in replacement["TARGETS"].values()
            )
        )

    def test_deployed_regime_policy_state_is_invalidated_before_fresh_analysis(self):
        rules = dca_config.default_rules_map()
        old_state = dca_config.empty_analysis_state(rules, now=NOW)
        old_policy = "multi-window-3-5-7-14-30-45-60-v2"
        old_state["POLICY_VERSION"] = old_policy
        for decision in old_state["TARGETS"].values():
            decision["POLICY_VERSION"] = old_policy

        with patch.object(
            crypto_analysis,
            "DCA_ANALYSIS_STATE_ENV",
            json.dumps(old_state),
        ):
            replacement = crypto_analysis._existing_or_empty_state(rules, NOW)

        self.assertEqual(
            replacement["POLICY_VERSION"], dca_config.TIMING_POLICY_VERSION
        )
        self.assertNotEqual(replacement["POLICY_VERSION"], old_policy)

    def test_uptrend_selects_lower_budget_tier(self):
        rules = dca_config.default_rules_map()["BTC_GBP"]
        daily, weekly = trend_rows("up")
        with (
            patch.object(crypto_analysis, "_fetch_asset_rows", return_value=(daily, weekly, intraday_rows(), ready_history())),
            patch.object(crypto_analysis, "LOCAL_TZ", "Asia/Bangkok"),
        ):
            decision = crypto_analysis.analyze_asset(
                MagicMock(), "BTC_GBP", rules, now=NOW
            )
        self.assertEqual(decision["REGIME"], "UPTREND")
        self.assertEqual(decision["AMOUNT_TIER"], "LOW")
        self.assertEqual(decision["ANALYSIS_STATUS"], "READY")
        self.assertEqual(
            datetime.fromisoformat(decision["VALID_UNTIL"].replace("Z", "+00:00"))
            - datetime.fromisoformat(decision["EXECUTE_AT"].replace("Z", "+00:00")),
            timedelta(minutes=60),
        )

    def test_active_override_forces_effective_uptrend_budget_on_normal_downtrend(self):
        rule = dca_config.default_rules_map()["BTC_GBP"]
        daily, weekly = trend_rows("down")
        entry = uptrend_override_state()["TARGETS"]["BTC_GBP"]
        with (
            patch.object(
                crypto_analysis,
                "_fetch_asset_rows",
                return_value=(daily, weekly, intraday_rows(), ready_history()),
            ),
            patch.object(crypto_analysis, "LOCAL_TZ", "Asia/Bangkok"),
        ):
            decision = crypto_analysis.analyze_asset(
                MagicMock(),
                "BTC_GBP",
                rule,
                now=NOW,
                uptrend_override=entry,
            )

        self.assertEqual(decision["REGIME"], "UPTREND")
        self.assertEqual(decision["AMOUNT_TIER"], "LOW")
        self.assertEqual(
            decision["SIGNALS"]["REGIME_WITHOUT_OVERRIDE"], "DOWNTREND"
        )
        self.assertTrue(decision["SIGNALS"]["UPTREND_OVERRIDE_APPLIED"])
        report = crypto_analysis._decision_report("BTC_GBP", decision, rule)
        self.assertIn("Emergency override: `ACTIVE`", report)
        self.assertIn("normal regime `DOWNTREND`", report)

    def test_eth_analysis_uses_completed_kraken_eth_gbp_history(self):
        rule = dca_config.default_rules_map()["ETH_GBP"]
        # The exact 170 candles required by SMA150's 20-day slope remain
        # sufficient for every configured production market.
        daily, weekly = trend_rows("down", count=190)
        exchange = MagicMock()
        with (
            patch.object(
                crypto_analysis,
                "_fetch_asset_rows",
                return_value=(daily, weekly, intraday_rows("05:00"), ready_history()),
            ) as fetch_rows,
            patch.object(crypto_analysis, "LOCAL_TZ", "Asia/Bangkok"),
        ):
            decision = crypto_analysis.analyze_asset(
                exchange, "ETH_GBP", rule, now=NOW
            )

        fetch_rows.assert_called_once_with(exchange, "ETH/GBP")
        self.assertEqual(decision["REGIME"], "DOWNTREND")
        self.assertEqual(decision["AMOUNT_TIER"], "HIGH")
        self.assertEqual(decision["TIMING"]["HISTORY_CANDLES"], 5760)

    def test_sideways_selects_and_reports_derived_midpoint(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 15},
            "BUY_ENABLED": True,
        }
        daily, weekly = trend_rows("up")
        with (
            patch.object(
                crypto_analysis,
                "_fetch_asset_rows",
                return_value=(daily, weekly, intraday_rows(), ready_history()),
            ),
            patch.object(
                crypto_analysis,
                "classify_trend",
                return_value=("SIDEWAYS", {"SOURCE": "test"}),
            ),
            patch.object(crypto_analysis, "LOCAL_TZ", "Asia/Bangkok"),
        ):
            decision = crypto_analysis.analyze_asset(
                MagicMock(), "ETH_GBP", rule, now=NOW
            )

        self.assertEqual(decision["AMOUNT_TIER"], "MID")
        report = crypto_analysis._decision_report("ETH_GBP", decision, rule)
        self.assertIn("`MID` tier (`£12.5` configured)", report)

    def test_analysis_failure_creates_fresh_non_executable_error(self):
        rule = dca_config.default_rules_map()["BTC_GBP"]
        first = crypto_analysis.error_decision("BTC_GBP", rule, "stale data", now=NOW)
        second = crypto_analysis.error_decision("BTC_GBP", rule, "stale data", now=NOW)
        self.assertEqual(first["ANALYSIS_STATUS"], "ERROR")
        self.assertIsNone(first["EXECUTE_AT"])
        self.assertEqual(first["DECISION_ID"], second["DECISION_ID"])

    def test_gemini_can_only_explain_and_cannot_select_outputs(self):
        response = MagicMock()
        response.text = "The Python decision follows the confirmed trend and timing data."
        client = MagicMock()
        client.__enter__.return_value = client
        client.models.generate_content.return_value = response

        with (
            patch.object(crypto_analysis, "GEMINI_API_KEY", "test-key"),
            patch.object(crypto_analysis.genai, "Client", return_value=client),
        ):
            summary, selected_time, model = crypto_analysis.get_ai_summary(
                "UPTREND at 03:00", "BTC/GBP"
            )

        self.assertIsNone(selected_time)
        self.assertEqual(model, "gemini-3.5-flash-lite")
        self.assertIn("Python decision", summary)
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Do not recommend or select", prompt)
        client.models.generate_content.assert_called_once_with(
            model="gemini-3.5-flash-lite", contents=ANY
        )

    def test_discord_failure_does_not_log_secret_webhook_url(self):
        secret_url = "https://discord.com/api/webhooks/id/secret-token"
        with (
            patch.object(crypto_analysis, "DISCORD_WEBHOOK_URL", secret_url),
            patch.object(
                crypto_analysis.requests,
                "post",
                side_effect=crypto_analysis.requests.Timeout(secret_url),
            ),
            patch("builtins.print") as output,
        ):
            self.assertFalse(crypto_analysis.send_to_discord("report"))
        rendered = " ".join(
            " ".join(str(arg) for arg in call.args) for call in output.call_args_list
        )
        self.assertNotIn(secret_url, rendered)
        self.assertNotIn("secret-token", rendered)


if __name__ == "__main__":
    unittest.main()

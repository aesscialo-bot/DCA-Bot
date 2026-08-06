import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import discord_bot
from dca_config import ANALYSIS_STATE_VERSION, ALLOWED_TARGETS, rules_hash


NOW = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


class MessageStub:
    def __init__(self, user_id="123"):
        self.replies = []
        self.author = SimpleNamespace(id=user_id)

    async def reply(self, content):
        self.replies.append(content)


def rules(*, enabled=(), low=10, up=20):
    return {
        symbol: {
            "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": up},
            "BUY_ENABLED": symbol in set(enabled),
        }
        for symbol in ALLOWED_TARGETS
    }


def analysis_state(
    live_rules,
    *,
    execute_offsets=None,
    status_overrides=None,
    generated_at=NOW,
):
    execute_offsets = execute_offsets or {
        symbol: 30 + index * 15 for index, symbol in enumerate(ALLOWED_TARGETS)
    }
    status_overrides = status_overrides or {}
    targets = {}
    for symbol in ALLOWED_TARGETS:
        status = status_overrides.get(symbol, "READY")
        execute_at = generated_at + timedelta(minutes=execute_offsets[symbol])
        targets[symbol] = {
            "STATUS": status,
            "REGIME": "UPTREND" if status == "READY" else None,
            "AMOUNT_TIER": "LOW" if status == "READY" else None,
            "EXECUTE_AT": execute_at.isoformat().replace("+00:00", "Z")
            if status == "READY"
            else None,
            "VALID_UNTIL": (execute_at + timedelta(minutes=60))
            .isoformat()
            .replace("+00:00", "Z")
            if status == "READY"
            else None,
            "DECISION_ID": f"decision-{symbol.lower()}",
            "RULES_HASH": rules_hash(symbol, live_rules[symbol]),
            "SIGNALS": {},
            "TIMING": {
                "ANALYZED_AT": generated_at.isoformat().replace("+00:00", "Z")
            },
        }
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_at.isoformat().replace("+00:00", "Z"),
        "TARGETS": targets,
    }


def variable_reader(live_rules, analysis, execution=None):
    values = {
        discord_bot.RULES_VARIABLE: json.dumps(live_rules),
        discord_bot.ANALYSIS_STATE_VARIABLE: json.dumps(analysis),
        discord_bot.EXECUTION_STATE_VARIABLE: json.dumps(execution or {}),
    }
    return lambda name: values.get(name)


def gist_delivery(
    delivery_id="OUF4EM-FRGI2-MQMWZD",
    *,
    symbol="BTC",
    created_at="2026-08-06T01:05:00Z",
):
    row = (
        "| 2026-08-06 08:05 +07 | GBP 10.00 | 1.300000 | USD 13.0000 | "
        "USD 12.9000 | GBP equivalent 0.08 | USD 64,500.0000 | "
        f"0.00020000 {symbol} | FUNDING-1 | {delivery_id} | "
        "optional/not saved |\n"
    )
    return {
        "version": 1,
        "delivery_id": delivery_id,
        "created_at": created_at,
        "symbol": symbol,
        "row": row,
        "row_sha256": sha256(row.encode("utf-8")).hexdigest(),
    }


class DiscordBotControlTests(unittest.TestCase):
    def setUp(self):
        self.allowlist = patch.object(discord_bot, "ALLOWED_USERS", "123")
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)
        discord_bot._pending_enable_confirmations.clear()
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._dca_schedule.clear()
        discord_bot._pending_recovery_symbols.clear()
        discord_bot._pending_gist_delivery_symbols.clear()
        discord_bot._awaiting_start_day_symbols.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        discord_bot._schedule_start_date = None
        self.rules = rules()
        self.analysis = analysis_state(self.rules)

    def test_only_three_production_usd_assets_are_accepted(self):
        self.assertEqual(discord_bot._normalise_usd_key("bitcoin"), "BTC_USD")
        self.assertEqual(discord_bot._normalise_usd_key("HYPE/USD"), "HYPE_USD")
        with self.assertRaisesRegex(ValueError, "Only BTC/USD"):
            discord_bot._normalise_usd_key("BTC/GBP")
        with self.assertRaisesRegex(ValueError, "Supported assets"):
            discord_bot._normalise_usd_key("CAR")

    def test_symbols_are_derived_from_valid_three_target_map(self):
        with patch.object(
            discord_bot,
            "_get_repo_variable_and_refresh",
            return_value=json.dumps(self.rules),
        ):
            self.assertEqual(
                discord_bot._symbols_from_dca_map(),
                "BTC/USD, HYPE/USD, SOL/USD",
            )

    def test_amount_update_is_atomic_and_requires_disabled_target(self):
        message = MessageStub()
        with (
            patch.object(
                discord_bot, "get_repo_variable", return_value=json.dumps(self.rules)
            ),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.handle_set_amounts("BTC", 10, 20, message))
        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_amounts",
                "symbol": "BTC_USD",
                "low_amount_gbp_json": "10.0",
                "up_amount_gbp_json": "20.0",
            },
        )
        self.assertIn("atomic budgets", message.replies[-1])
        self.assertIn("sideways midpoint £15", message.replies[-1])

        enabled_rules = rules(enabled={"BTC_USD"})
        blocked = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                return_value=json.dumps(enabled_rules),
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(discord_bot.handle_set_amounts("BTC", 11, 21, blocked))
        dispatch.assert_not_called()
        self.assertIn("disable", blocked.replies[-1])

    def test_zero_is_allowed_only_as_disabled_placeholder(self):
        message = MessageStub()
        with (
            patch.object(
                discord_bot, "get_repo_variable", return_value=json.dumps(self.rules)
            ),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.handle_set_amounts("BTC", 0, 0, message))
        dispatch.assert_called_once()
        with self.assertRaisesRegex(ValueError, "£0 or"):
            discord_bot._parse_amount(4.99, "LOW amount")

    def test_budget_command_rejects_lower_amount_above_higher_amount(self):
        message = MessageStub()
        with patch.object(discord_bot, "trigger_workflow") as dispatch:
            asyncio.run(discord_bot.handle_set_amounts("BTC", 20, 10, message))
        dispatch.assert_not_called()
        self.assertIn("lower amount must not exceed", message.replies[-1])

    def test_write_safety_is_allowlisted_and_exact_prefix(self):
        message = MessageStub()
        with patch.object(discord_bot, "ALLOWED_USERS", ""):
            reason = discord_bot._config_write_block_reason(
                "set_amounts", "!dca set BTC amounts to 10 low and 20 up", message
            )
        self.assertIn("DISCORD_ALLOWED_USERS", reason)
        for text in (
            "dca disable BTC",
            "!DCA disable BTC",
            " !dca disable BTC",
        ):
            with self.subTest(text=text):
                reason = discord_bot._config_write_block_reason(
                    "set_enabled", text, message
                )
                self.assertIn("start exactly", reason)

    def test_exact_command_parser_rejects_near_miss(self):
        message = MessageStub()
        with patch.object(discord_bot, "handle_set_amounts") as handler:
            handled = asyncio.run(
                discord_bot._handle_exact_dca_command(
                    "!dca set BTC amount to 10", message
                )
            )
        self.assertTrue(handled)
        handler.assert_not_called()
        self.assertIn("Unrecognized exact", message.replies[-1])

    def test_budget_command_accepts_clear_high_word_and_legacy_up_alias(self):
        self.assertIsNotNone(
            discord_bot._SET_AMOUNTS_RE.fullmatch(
                "!dca set BTC amounts to 10 low and 20 high"
            )
        )
        self.assertIsNotNone(
            discord_bot._SET_AMOUNTS_RE.fullmatch(
                "!dca set BTC amounts to 10 low and 20 up"
            )
        )

    def test_disable_uses_serialized_writer_without_confirmation(self):
        message = MessageStub()
        with patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch:
            asyncio.run(discord_bot.handle_disable("HYPE", message))
        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled",
                "symbol": "HYPE_USD",
                "enabled_json": "false",
            },
        )

    def test_enable_review_contains_all_safety_information(self):
        message = MessageStub()
        reader = variable_reader(self.rules, self.analysis)
        with (
            patch.object(discord_bot, "get_repo_variable", side_effect=reader),
            patch.object(discord_bot, "datetime", FrozenDateTime),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))
        reply = message.replies[-1]
        self.assertIn("UPTREND/lower: £10", reply)
        self.assertIn("SIDEWAYS/midpoint: £15", reply)
        self.assertIn("DOWNTREND/higher: £20", reply)
        self.assertIn("Latest regime", reply)
        self.assertIn("Effective amount: £10", reply)
        self.assertIn("Next execution", reply)
        self.assertIn("Decision age", reply)
        self.assertIn("Maximum aggregate daily exposure", reply)
        self.assertIn("Kraken's current market minimum", reply)
        self.assertIn("!dca confirm enable BTC_USD", reply)

    def test_enable_requires_nonzero_budgets_and_fresh_matching_decision(self):
        zero_rules = rules(low=0, up=0)
        message = MessageStub()
        with patch.object(
            discord_bot,
            "get_repo_variable",
            side_effect=variable_reader(zero_rules, analysis_state(zero_rules)),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))
        self.assertIn("must be between £5", message.replies[-1])

        stale_analysis = analysis_state(
            self.rules, generated_at=NOW - timedelta(days=2)
        )
        stale = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, stale_analysis),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", stale))
        self.assertIn("stale", stale.replies[-1])

    def test_exact_enable_confirmation_binds_decision_and_dispatches_live_check(self):
        message = MessageStub()
        reader = variable_reader(self.rules, self.analysis)
        with (
            patch.object(discord_bot, "get_repo_variable", side_effect=reader),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))
            dispatch.assert_not_called()
            asyncio.run(
                discord_bot._handle_enable_confirmation(
                    message, "!dca confirm enable BTC_USD"
                )
            )
        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled",
                "symbol": "BTC_USD",
                "enabled_json": "true",
                "expected_rules_hash": rules_hash("BTC_USD", self.rules["BTC_USD"]),
                "expected_decision_id": "decision-btc_usd",
                "expected_global_rules_hash": discord_bot.global_rules_pre_state_hash(
                    self.rules
                ),
            },
        )
        self.assertIn("Kraken minimum", message.replies[-1])
        self.assertNotIn("123", discord_bot._pending_enable_confirmations)

    def test_enable_review_rejects_pending_order_for_any_asset(self):
        execution = {
            "SOL_USD": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "funding_client_order_id": "dca-fedcba09876543",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-sol_usd",
                    "created_at": "2026-08-05T03:55:00Z",
                },
            }
        }
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, self.analysis, execution),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))
        dispatch.assert_not_called()
        self.assertIn("reconciliation is pending for SOL_USD", message.replies[-1])

    def test_enable_review_allows_pending_portfolio_ledger_delivery(self):
        execution = {
            "SOL_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery("ORDER-SOL-1", symbol="SOL")
                ],
            }
        }

        review = discord_bot._enable_review(
            "BTC_USD",
            self.rules,
            self.analysis,
            execution,
            now=NOW,
        )

        self.assertEqual(review["symbol"], "BTC_USD")

    def test_confirmation_fails_if_live_decision_changes(self):
        message = MessageStub()
        first_reader = variable_reader(self.rules, self.analysis)
        with (
            patch.object(discord_bot, "get_repo_variable", side_effect=first_reader),
            patch.object(discord_bot, "datetime", FrozenDateTime),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))

        changed = deepcopy(self.analysis)
        changed["TARGETS"]["BTC_USD"]["DECISION_ID"] = "different-decision"
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, changed),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot._handle_enable_confirmation(
                    message, "!dca confirm enable BTC_USD"
                )
            )
        dispatch.assert_not_called()
        self.assertIn("decision", message.replies[-1])

    def test_confirmation_rejects_global_rule_change_with_same_target_exposure(self):
        analysis = deepcopy(self.analysis)
        analysis["TARGETS"]["SOL_USD"].update(
            {
                "STATUS": "ERROR",
                "REGIME": None,
                "AMOUNT_TIER": None,
                "EXECUTE_AT": None,
                "VALID_UNTIL": None,
                "SIGNALS": {"ERROR": "test"},
            }
        )
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, analysis),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))

        changed_rules = deepcopy(self.rules)
        # SOL remains disabled and its maximum budget remains £20, so aggregate
        # exposure and every BTC-bound field are unchanged. Only the global
        # canonical pre-state detects this concurrent edit.
        changed_rules["SOL_USD"]["REGIME_AMOUNTS_GBP"]["LOW"] = 11
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(changed_rules, analysis),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot._handle_enable_confirmation(
                    message, "!dca confirm enable BTC_USD"
                )
            )
        dispatch.assert_not_called()
        self.assertIn("global three-asset DCA rules changed", message.replies[-1])

    def test_analyze_exact_asset_or_all(self):
        message = MessageStub()
        with patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch:
            asyncio.run(discord_bot.handle_analyze({"symbol": "SOL"}, message))
            asyncio.run(discord_bot.handle_analyze({"symbol": "all"}, message))
        self.assertEqual(
            dispatch.call_args_list[0].args,
            ("crypto_analysis.yml", {"symbol": "SOL/USD"}),
        )
        self.assertEqual(
            dispatch.call_args_list[1].args,
            ("crypto_analysis.yml", {"symbol": "all"}),
        )

    def test_status_and_health_report_ready_but_disabled(self):
        reader = variable_reader(self.rules, self.analysis)
        status_message = MessageStub()
        health_message = MessageStub()
        with (
            patch.object(discord_bot, "get_repo_variable", side_effect=reader),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_status({}, status_message))
        self.assertIn("BTC_USD", status_message.replies[-1])
        self.assertIn("UPTREND/lower £10", status_message.replies[-1])
        self.assertIn("SIDEWAYS/midpoint £15", status_message.replies[-1])
        self.assertIn("DOWNTREND/higher £20", status_message.replies[-1])
        self.assertIn("ready-but-disabled", status_message.replies[-1])

        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, self.analysis),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_health({}, health_message))
        self.assertIn("READY-BUT-DISABLED", health_message.replies[-1])
        self.assertIn("fresh READY 3/3", health_message.replies[-1])
        self.assertIn("Buy-enabled targets: 0/3", health_message.replies[-1])

    def test_status_and_health_separate_portfolio_delivery_from_kraken_recovery(self):
        execution = {
            "BTC_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery(),
                    gist_delivery(
                        "OUF4EM-FRGI2-MQMWZE",
                        created_at="2026-08-06T01:06:00Z",
                    ),
                ],
            }
        }
        status_message = MessageStub()
        health_message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, self.analysis, execution),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_status({}, status_message))
        self.assertIn(
            "PORTFOLIO LEDGER DELIVERY WARNING (2 pending)",
            status_message.replies[-1],
        )
        self.assertIn("2 pending record(s)", status_message.replies[-1])

        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, self.analysis, execution),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_health({}, health_message))
        self.assertIn("DCA health: ATTENTION REQUIRED", health_message.replies[-1])
        self.assertIn("pending Kraken recoveries 0", health_message.replies[-1])
        self.assertIn(
            "Portfolio ledger delivery: WARNING; 2 pending record(s)",
            health_message.replies[-1],
        )

    def test_health_reports_armed_while_waiting_for_start_day_analysis(self):
        live_rules = rules(enabled=set(ALLOWED_TARGETS))
        decisions = analysis_state(
            live_rules,
            status_overrides={symbol: "ERROR" for symbol in ALLOWED_TARGETS},
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=NOW,
            )
        )
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(live_rules, decisions),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_health({}, message))

        self.assertIn("DCA health: ARMED", message.replies[-1])
        self.assertIn("awaiting 04:00 start-day analysis", message.replies[-1])
        self.assertNotIn("ATTENTION REQUIRED", message.replies[-1])
        self.assertNotIn("Analysis ERROR", message.replies[-1])

    def test_health_reports_armed_for_ready_decisions_that_predate_start(self):
        live_rules = rules(enabled=set(ALLOWED_TARGETS))
        decisions = analysis_state(live_rules)
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=NOW,
            )
        )
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(live_rules, decisions),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_health({}, message))

        self.assertIn("DCA health: ARMED", message.replies[-1])
        self.assertIn("active targets 0", message.replies[-1])

    def test_invalid_state_reports_not_ready_without_echoing_json(self):
        message = MessageStub()
        bad_rules = '{"BTC_THB":{"TOKEN":"do-not-echo"}}'
        with patch.object(
            discord_bot,
            "get_repo_variable",
            side_effect=lambda name: bad_rules
            if name == discord_bot.RULES_VARIABLE
            else "{}",
        ):
            asyncio.run(discord_bot.handle_health({}, message))
        self.assertIn("NOT READY", message.replies[-1])
        self.assertNotIn("do-not-echo", message.replies[-1])


class DiscordBotSchedulerTests(unittest.TestCase):
    def setUp(self):
        discord_bot._dca_schedule.clear()
        discord_bot._pending_recovery_symbols.clear()
        discord_bot._pending_gist_delivery_symbols.clear()
        discord_bot._awaiting_start_day_symbols.clear()
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        discord_bot._schedule_start_date = None

    def test_v1_analysis_state_clears_schedule_and_fails_closed(self):
        live_rules = rules(enabled={"BTC_USD", "HYPE_USD", "SOL_USD"})
        decisions = analysis_state(live_rules)
        decisions["VERSION"] = 1

        self.assertFalse(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-07",
                now=NOW,
            )
        )
        self.assertEqual(discord_bot._dca_schedule, {})
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), [])
        self.assertIn("VERSION must be 2", discord_bot._schedule_error)

    def test_multiple_assets_can_share_or_use_different_absolute_times(self):
        live_rules = rules(enabled={"BTC_USD", "HYPE_USD", "SOL_USD"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={
                "BTC_USD": 30,
                "HYPE_USD": 30,
                "SOL_USD": 45,
            },
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(
            set(discord_bot._dca_schedule),
            {"BTC_USD", "HYPE_USD", "SOL_USD"},
        )
        self.assertEqual(
            discord_bot._dca_schedule["BTC_USD"]["execute_at"],
            discord_bot._dca_schedule["HYPE_USD"]["execute_at"],
        )
        self.assertNotEqual(
            discord_bot._dca_schedule["BTC_USD"]["execute_at"],
            discord_bot._dca_schedule["SOL_USD"]["execute_at"],
        )

    def test_scheduler_arms_without_warning_before_start_day_analysis(self):
        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(live_rules)

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=NOW,
            )
        )
        self.assertEqual(discord_bot._dca_schedule, {})
        self.assertIsNone(discord_bot._schedule_warning)

    def test_missing_start_day_analysis_alerts_after_bounded_grace(self):
        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_USD": "ERROR"}
        )
        before_deadline = datetime(2026, 8, 5, 21, 14, tzinfo=timezone.utc)
        after_deadline = datetime(2026, 8, 5, 21, 15, tzinfo=timezone.utc)

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=before_deadline,
            )
        )
        self.assertIsNone(discord_bot._schedule_warning)

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=after_deadline,
            )
        )
        self.assertIn("BTC_USD: analysis ERROR", discord_bot._schedule_warning)

    def test_fresh_start_day_analysis_schedules_during_grace(self):
        live_rules = rules(enabled={"BTC_USD"})
        analysis_time = datetime(2026, 8, 5, 21, 1, tzinfo=timezone.utc)
        decisions = analysis_state(live_rules, generated_at=analysis_time)

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=analysis_time,
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"BTC_USD"})
        self.assertIsNone(discord_bot._schedule_warning)

        with patch.object(discord_bot, "datetime", FrozenDateTime):
            FrozenDateTime.current = analysis_time
            try:
                self.assertNotIn(
                    "awaiting 04:00", discord_bot._format_cron_status()
                )
            finally:
                FrozenDateTime.current = NOW

    def test_fresh_start_day_analysis_error_alerts_during_grace(self):
        live_rules = rules(enabled={"BTC_USD"})
        analysis_time = datetime(2026, 8, 5, 21, 1, tzinfo=timezone.utc)
        decisions = analysis_state(
            live_rules,
            generated_at=analysis_time,
            status_overrides={"BTC_USD": "ERROR"},
        )

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=analysis_time,
            )
        )
        self.assertIn("BTC_USD: analysis ERROR", discord_bot._schedule_warning)
        self.assertEqual(discord_bot._awaiting_start_day_symbols, set())

    def test_disabled_asset_rules_mismatch_does_not_block_enabled_asset(self):
        live_rules = rules(enabled={"HYPE_USD"})
        decisions = analysis_state(live_rules)
        live_rules["BTC_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 11, "UP": 21}

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"HYPE_USD"})

    def test_enabled_error_asset_is_skipped_without_blocking_ready_asset(self):
        live_rules = rules(enabled={"BTC_USD", "HYPE_USD"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_USD": "ERROR"}
        )

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"HYPE_USD"})
        self.assertIn("BTC_USD: analysis ERROR", discord_bot._schedule_warning)

    def test_due_assets_use_inclusive_minus_five_plus_sixty_window(self):
        live_rules = rules(enabled={"BTC_USD", "HYPE_USD"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={symbol: 30 for symbol in ALLOWED_TARGETS},
        )
        discord_bot.refresh_dca_schedule(
            json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=25)),
            ["BTC_USD", "HYPE_USD"],
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=91)), []
        )

    def test_scheduler_fails_closed_on_invalid_map_or_enabled_error_decision(self):
        self.assertFalse(discord_bot.refresh_dca_schedule("{}", "{}", "{}"))
        self.assertEqual(discord_bot._dca_schedule, {})
        self.assertIsNotNone(discord_bot._schedule_error)

        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_USD": "ERROR"}
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertIn("analysis ERROR", discord_bot._schedule_warning)

        stale = analysis_state(
            live_rules, generated_at=NOW - timedelta(days=2)
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(stale), "{}", now=NOW
            )
        )
        self.assertIn("stale decision", discord_bot._schedule_warning)

    def test_start_date_blocks_new_dispatches_until_local_date(self):
        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={symbol: 30 for symbol in ALLOWED_TARGETS},
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-06",
                now=NOW,
            )
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=25)), []
        )
        self.assertEqual(discord_bot._schedule_start_date.isoformat(), "2026-08-06")

    def test_invalid_start_date_fails_closed(self):
        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(live_rules)
        self.assertFalse(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "06-08-2026",
                now=NOW,
            )
        )
        self.assertIn("YYYY-MM-DD", discord_bot._schedule_error)

    def test_pending_intent_dispatches_recovery_even_when_disabled(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "SOL_USD": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "decision_id": "decision-sol_usd",
                    "client_order_id": "dca-1234567890abcd",
                    "funding_client_order_id": "dca-fedcba09876543",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 20,
                    "created_at": "2026-08-05T03:55:00Z",
                },
            }
        }
        discord_bot.refresh_dca_schedule(
            json.dumps(live_rules),
            json.dumps(decisions),
            json.dumps(execution),
            now=NOW,
        )
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), ["SOL_USD"])

    def test_pending_gist_delivery_dispatches_without_kraken_recovery(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "SOL_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery("ORDER-SOL-1", symbol="SOL")
                ],
            }
        }

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                json.dumps(execution),
                now=NOW,
            )
        )

        self.assertEqual(discord_bot._pending_recovery_symbols, set())
        self.assertEqual(discord_bot._pending_gist_delivery_symbols, {"SOL_USD"})
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), ["SOL_USD"])

    def test_pending_gist_delivery_does_not_block_new_order_schedule(self):
        live_rules = rules(enabled={"BTC_USD"})
        decisions = analysis_state(live_rules)
        execution = {
            "SOL_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery("ORDER-SOL-1", symbol="SOL")
                ],
            }
        }

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                json.dumps(execution),
                now=NOW,
            )
        )

        self.assertEqual(set(discord_bot._dca_schedule), {"BTC_USD"})
        self.assertIsNone(discord_bot._schedule_warning)

    def test_pending_gist_delivery_survives_invalid_analysis_and_dispatches(self):
        live_rules = rules()
        execution = {
            "BTC_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [gist_delivery()],
            }
        }
        self.assertFalse(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), "{}", json.dumps(execution), now=NOW
            )
        )
        self.assertEqual(discord_bot._pending_recovery_symbols, set())
        self.assertEqual(discord_bot._pending_gist_delivery_symbols, {"BTC_USD"})
        with patch.object(discord_bot, "DCA_CRON_ENABLED", True):
            self.assertIn(
                "portfolio ledger delivery", discord_bot._format_cron_status()
            )

        FrozenDateTime.current = NOW
        with (
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
        dispatch.assert_called_once_with(
            "daily_dca.yml", {"symbols_json": '["BTC_USD"]'}
        )

    def test_pending_gist_delivery_uses_thirty_minute_dispatch_guard(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "BTC_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [gist_delivery()],
            }
        }
        discord_bot.refresh_dca_schedule(
            json.dumps(live_rules),
            json.dumps(decisions),
            json.dumps(execution),
            now=NOW,
        )
        FrozenDateTime.current = NOW
        with (
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(
                discord_bot,
                "monotonic",
                side_effect=[0, 0, 1_799, 1_800, 1_800],
            ),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
            asyncio.run(discord_bot.dca_scheduler_tick.coro())

        self.assertEqual(dispatch.call_count, 2)
        dispatch.assert_called_with(
            "daily_dca.yml", {"symbols_json": '["BTC_USD"]'}
        )

    def test_pending_recovery_survives_invalid_analysis_state(self):
        live_rules = rules()
        execution = {
            "BTC_USD": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "decision_id": "decision-btc_usd",
                    "client_order_id": "dca-1234567890abcd",
                    "funding_client_order_id": "dca-fedcba09876543",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 20,
                    "created_at": "2026-08-05T03:55:00Z",
                },
            }
        }
        self.assertFalse(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), "{}", json.dumps(execution), now=NOW
            )
        )
        self.assertEqual(discord_bot._pending_recovery_symbols, {"BTC_USD"})

        FrozenDateTime.current = NOW
        with (
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
        dispatch.assert_called_once_with(
            "daily_dca.yml", {"symbols_json": '["BTC_USD"]'}
        )

    def test_scheduler_dispatches_only_due_symbols_and_sets_guard(self):
        now = NOW + timedelta(minutes=30)
        FrozenDateTime.current = now
        discord_bot._dca_schedule.update(
            {
                "BTC_USD": {
                    "execute_at": now.isoformat(),
                    "valid_until": (now + timedelta(minutes=60)).isoformat(),
                    "decision_id": "btc-decision",
                    "last_buy_date": "",
                },
                "SOL_USD": {
                    "execute_at": (now + timedelta(hours=2)).isoformat(),
                    "valid_until": (now + timedelta(hours=3)).isoformat(),
                    "decision_id": "sol-decision",
                    "last_buy_date": "",
                },
            }
        )
        with (
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
        dispatch.assert_called_once_with(
            "daily_dca.yml", {"symbols_json": '["BTC_USD"]'}
        )
        self.assertIn(("BTC_USD", "btc-decision"), discord_bot._dca_dispatch_guard)


class DiscordBotWorkflowAndGeminiTests(unittest.TestCase):
    @patch.object(discord_bot.requests, "post")
    def test_dispatch_uses_configured_workflow_ref(self, post):
        post.return_value.status_code = 204
        with patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"):
            result = discord_bot.trigger_workflow(
                "crypto_analysis.yml", {"symbol": "all"}
            )
        self.assertTrue(result)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"ref": "main", "inputs": {"symbol": "all"}},
        )

    @patch.object(discord_bot.requests, "post")
    def test_dispatch_fails_closed_without_ref(self, post):
        with patch.object(discord_bot, "GITHUB_WORKFLOW_REF", ""):
            self.assertFalse(discord_bot.trigger_workflow("daily_dca.yml"))
        post.assert_not_called()

    def test_gemini_is_limited_to_read_only_actions(self):
        self.assertEqual(
            discord_bot._validate_intent(
                {"action": "set_enabled", "params": {"symbol": "BTC"}}
            )["action"],
            "unknown",
        )
        response = MagicMock()
        response.text = json.dumps(
            {"action": "status", "params": {}, "reply": "Show status"}
        )
        client = MagicMock()
        client.__enter__.return_value = client
        client.models.generate_content.return_value = response
        with (
            patch.object(discord_bot, "GEMINI_API_KEY", "test-key"),
            patch.object(discord_bot.genai, "Client", return_value=client) as ctor,
        ):
            result = asyncio.run(discord_bot.classify_intent("show status"))
        ctor.assert_called_once_with(api_key="test-key")
        client.models.generate_content.assert_called_once_with(
            model="gemini-3.5-flash-lite", contents=ANY
        )
        self.assertEqual(result["action"], "status")


if __name__ == "__main__":
    unittest.main()

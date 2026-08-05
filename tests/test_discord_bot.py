import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import discord_bot
from dca_config import ALLOWED_TARGETS, rules_hash


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
            "AMOUNT_TIER": "UP" if status == "READY" else None,
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
        "VERSION": 1,
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


class DiscordBotControlTests(unittest.TestCase):
    def setUp(self):
        self.allowlist = patch.object(discord_bot, "ALLOWED_USERS", "123")
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)
        discord_bot._pending_enable_confirmations.clear()
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._dca_schedule.clear()
        discord_bot._pending_recovery_symbols.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        self.rules = rules()
        self.analysis = analysis_state(self.rules)

    def test_only_four_production_gbp_assets_are_accepted(self):
        self.assertEqual(discord_bot._normalise_gbp_key("bitcoin"), "BTC_GBP")
        self.assertEqual(discord_bot._normalise_gbp_key("ADA/GBP"), "ADA_GBP")
        with self.assertRaisesRegex(ValueError, "Only GBP"):
            discord_bot._normalise_gbp_key("BTC/USD")
        with self.assertRaisesRegex(ValueError, "Supported assets"):
            discord_bot._normalise_gbp_key("CAR")

    def test_symbols_are_derived_from_valid_four_target_map(self):
        with patch.object(
            discord_bot,
            "_get_repo_variable_and_refresh",
            return_value=json.dumps(self.rules),
        ):
            self.assertEqual(
                discord_bot._symbols_from_dca_map(),
                "BTC/GBP, ETH/GBP, SOL/GBP, ADA/GBP",
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
                "symbol": "BTC_GBP",
                "low_amount_gbp_json": "10.0",
                "up_amount_gbp_json": "20.0",
            },
        )
        self.assertIn("atomic budgets", message.replies[-1])

        enabled_rules = rules(enabled={"BTC_GBP"})
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

    def test_disable_uses_serialized_writer_without_confirmation(self):
        message = MessageStub()
        with patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch:
            asyncio.run(discord_bot.handle_disable("ETH", message))
        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled",
                "symbol": "ETH_GBP",
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
        self.assertIn("LOW: £10", reply)
        self.assertIn("UP: £20", reply)
        self.assertIn("Latest regime", reply)
        self.assertIn("Effective amount: £20", reply)
        self.assertIn("Next execution", reply)
        self.assertIn("Decision age", reply)
        self.assertIn("Maximum aggregate daily exposure", reply)
        self.assertIn("Kraken's current market minimum", reply)
        self.assertIn("!dca confirm enable BTC_GBP", reply)

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
                    message, "!dca confirm enable BTC_GBP"
                )
            )
        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled",
                "symbol": "BTC_GBP",
                "enabled_json": "true",
                "expected_rules_hash": rules_hash("BTC_GBP", self.rules["BTC_GBP"]),
                "expected_decision_id": "decision-btc_gbp",
                "expected_global_rules_hash": discord_bot.global_rules_pre_state_hash(
                    self.rules
                ),
            },
        )
        self.assertIn("Kraken minimum", message.replies[-1])
        self.assertNotIn("123", discord_bot._pending_enable_confirmations)

    def test_enable_review_rejects_pending_order_for_any_asset(self):
        execution = {
            "ADA_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-ada_gbp",
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
        self.assertIn("reconciliation is pending for ADA_GBP", message.replies[-1])

    def test_confirmation_fails_if_live_decision_changes(self):
        message = MessageStub()
        first_reader = variable_reader(self.rules, self.analysis)
        with (
            patch.object(discord_bot, "get_repo_variable", side_effect=first_reader),
            patch.object(discord_bot, "datetime", FrozenDateTime),
        ):
            asyncio.run(discord_bot.handle_enable("BTC", message))

        changed = deepcopy(self.analysis)
        changed["TARGETS"]["BTC_GBP"]["DECISION_ID"] = "different-decision"
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
                    message, "!dca confirm enable BTC_GBP"
                )
            )
        dispatch.assert_not_called()
        self.assertIn("decision", message.replies[-1])

    def test_confirmation_rejects_global_rule_change_with_same_target_exposure(self):
        analysis = deepcopy(self.analysis)
        analysis["TARGETS"]["ADA_GBP"].update(
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
        # ADA remains disabled and its maximum budget remains £20, so aggregate
        # exposure and every BTC-bound field are unchanged. Only the global
        # canonical pre-state detects this concurrent edit.
        changed_rules["ADA_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 11
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
                    message, "!dca confirm enable BTC_GBP"
                )
            )
        dispatch.assert_not_called()
        self.assertIn("global four-asset DCA rules changed", message.replies[-1])

    def test_analyze_exact_asset_or_all(self):
        message = MessageStub()
        with patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch:
            asyncio.run(discord_bot.handle_analyze({"symbol": "SOL"}, message))
            asyncio.run(discord_bot.handle_analyze({"symbol": "all"}, message))
        self.assertEqual(
            dispatch.call_args_list[0].args,
            ("crypto_analysis.yml", {"symbol": "SOL/GBP"}),
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
        self.assertIn("BTC_GBP", status_message.replies[-1])
        self.assertIn("LOW £10", status_message.replies[-1])
        self.assertIn("UP £20", status_message.replies[-1])
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
        self.assertIn("fresh READY 4/4", health_message.replies[-1])
        self.assertIn("Buy-enabled targets: 0/4", health_message.replies[-1])

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
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None

    def test_multiple_assets_can_share_or_use_different_absolute_times(self):
        live_rules = rules(enabled={"BTC_GBP", "ETH_GBP", "SOL_GBP"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={
                "BTC_GBP": 30,
                "ETH_GBP": 30,
                "SOL_GBP": 45,
                "ADA_GBP": 60,
            },
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(
            set(discord_bot._dca_schedule),
            {"BTC_GBP", "ETH_GBP", "SOL_GBP"},
        )
        self.assertEqual(
            discord_bot._dca_schedule["BTC_GBP"]["execute_at"],
            discord_bot._dca_schedule["ETH_GBP"]["execute_at"],
        )
        self.assertNotEqual(
            discord_bot._dca_schedule["BTC_GBP"]["execute_at"],
            discord_bot._dca_schedule["SOL_GBP"]["execute_at"],
        )

    def test_disabled_asset_rules_mismatch_does_not_block_enabled_asset(self):
        live_rules = rules(enabled={"ETH_GBP"})
        decisions = analysis_state(live_rules)
        live_rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 11, "UP": 21}

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"ETH_GBP"})

    def test_enabled_error_asset_is_skipped_without_blocking_ready_asset(self):
        live_rules = rules(enabled={"BTC_GBP", "ETH_GBP"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_GBP": "ERROR"}
        )

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"ETH_GBP"})
        self.assertIn("BTC_GBP: analysis ERROR", discord_bot._schedule_warning)

    def test_due_assets_use_inclusive_minus_five_plus_sixty_window(self):
        live_rules = rules(enabled={"BTC_GBP", "ETH_GBP"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={symbol: 30 for symbol in ALLOWED_TARGETS},
        )
        discord_bot.refresh_dca_schedule(
            json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=25)),
            ["BTC_GBP", "ETH_GBP"],
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=91)), []
        )

    def test_scheduler_fails_closed_on_invalid_map_or_enabled_error_decision(self):
        self.assertFalse(discord_bot.refresh_dca_schedule("{}", "{}", "{}"))
        self.assertEqual(discord_bot._dca_schedule, {})
        self.assertIsNotNone(discord_bot._schedule_error)

        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_GBP": "ERROR"}
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

    def test_pending_intent_dispatches_recovery_even_when_disabled(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "ADA_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "decision_id": "decision-ada_gbp",
                    "client_order_id": "dca-1234567890abcd",
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
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), ["ADA_GBP"])

    def test_pending_recovery_survives_invalid_analysis_state(self):
        live_rules = rules()
        execution = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "decision_id": "decision-btc_gbp",
                    "client_order_id": "dca-1234567890abcd",
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
        self.assertEqual(discord_bot._pending_recovery_symbols, {"BTC_GBP"})

        FrozenDateTime.current = NOW
        with (
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
        ):
            asyncio.run(discord_bot.dca_scheduler_tick.coro())
        dispatch.assert_called_once_with(
            "daily_dca.yml", {"symbols_json": '["BTC_GBP"]'}
        )

    def test_scheduler_dispatches_only_due_symbols_and_sets_guard(self):
        now = NOW + timedelta(minutes=30)
        FrozenDateTime.current = now
        discord_bot._dca_schedule.update(
            {
                "BTC_GBP": {
                    "execute_at": now.isoformat(),
                    "valid_until": (now + timedelta(minutes=60)).isoformat(),
                    "decision_id": "btc-decision",
                    "last_buy_date": "",
                },
                "SOL_GBP": {
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
            "daily_dca.yml", {"symbols_json": '["BTC_GBP"]'}
        )
        self.assertIn(("BTC_GBP", "btc-decision"), discord_bot._dca_dispatch_guard)


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
            model="gemini-2.5-flash-lite", contents=ANY
        )
        self.assertEqual(result["action"], "status")


if __name__ == "__main__":
    unittest.main()

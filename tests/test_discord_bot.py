import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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


class TypingStub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ChannelStub:
    def __init__(self, channel_id="456"):
        self.id = channel_id

    def typing(self):
        return TypingStub()


class MessageStub:
    def __init__(self, user_id="123", *, content="", channel_id="456"):
        self.replies = []
        self.reply_kwargs = []
        self.author = SimpleNamespace(id=user_id)
        self.content = content
        self.channel = ChannelStub(channel_id)
        self.mentions = []

    async def reply(self, content, **kwargs):
        self.replies.append(content)
        self.reply_kwargs.append(kwargs)


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


class DiscordBotControlTests(unittest.TestCase):
    def setUp(self):
        self.allowlist = patch.object(discord_bot, "ALLOWED_USERS", "123")
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)
        discord_bot._pending_enable_confirmations.clear()
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._dca_schedule.clear()
        discord_bot._pending_recovery_symbols.clear()
        discord_bot._awaiting_start_day_symbols.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        discord_bot._schedule_start_date = None
        self.rules = rules()
        self.analysis = analysis_state(self.rules)

    def test_only_three_production_usd_assets_are_accepted(self):
        self.assertEqual(discord_bot._normalise_usd_key("bitcoin"), "BTC_USD")
        self.assertEqual(discord_bot._normalise_usd_key("hyperliquid"), "HYPE_USD")
        self.assertEqual(discord_bot._normalise_usd_key("solana"), "SOL_USD")
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
        self.assertIn("Budget update queued", message.replies[-1])
        self.assertIn("not applied yet", message.replies[-1])
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
        self.assertIn("Command not accepted", message.replies[-1])
        self.assertIn("no changes were made", message.replies[-1])

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
        self.assertNotIn("✅ ENABLED", status_message.replies[-1])
        self.assertLessEqual(len(status_message.replies[-1]), 1_990)

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
        self.assertIn("Gemini chat:", health_message.replies[-1])
        self.assertNotIn("✅ Buy-enabled targets", health_message.replies[-1])

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


class DiscordBotHelpAndChatTests(unittest.TestCase):
    def setUp(self):
        discord_bot._chat_histories.clear()
        discord_bot._chat_history_updated_at.clear()

    def test_help_documents_every_supported_command_and_safety_boundary(self):
        required = (
            "show status",
            "!dca status",
            "!dca health",
            "show portfolio",
            "!dca portfolio",
            "!dca help",
            "!dca analyze BTC",
            "!dca analyze all",
            "!dca set BTC amounts to 10 low and 20 high",
            "!dca disable BTC",
            "!dca enable BTC",
            "!dca confirm enable BTC_USD",
            "bitcoin",
            "hyperliquid",
            "solana",
            "Legacy `up`",
            "Queued does not mean applied",
            "Natural language never",
            "same user",
            "requires Gemini",
            "configured channel and allowlist gate all replies",
            "use a DM or mention the bot",
            "Every `!dca` form requires exact lowercase words and internal spacing",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, discord_bot.HELP_TEXT)
        self.assertLessEqual(len(discord_bot.HELP_TEXT), 1_990)

    def test_exact_read_only_command_aliases_route_without_gemini(self):
        cases = (
            ("!dca help", "handle_help"),
            ("!dca status", "handle_status"),
            ("!dca health", "handle_health"),
            ("!dca portfolio", "handle_portfolio"),
        )
        for command, handler_name in cases:
            with self.subTest(command=command):
                message = MessageStub(content=command)
                with patch.object(
                    discord_bot, handler_name, new_callable=AsyncMock
                ) as handler:
                    handled = asyncio.run(
                        discord_bot._handle_exact_dca_command(command, message)
                    )
                self.assertTrue(handled)
                handler.assert_awaited_once_with({}, message)

    def test_every_exact_write_and_analysis_form_routes_deterministically(self):
        cases = (
            (
                "!dca set Bitcoin amounts to 10 low and 20 high",
                "handle_set_amounts",
                ("Bitcoin", "10", "20"),
            ),
            (
                "!dca set HYPE amounts to 5 low and 15 up",
                "handle_set_amounts",
                ("HYPE", "5", "15"),
            ),
            ("!dca disable bitcoin", "handle_disable", ("bitcoin",)),
            ("!dca enable Hyperliquid", "handle_enable", ("Hyperliquid",)),
            ("!dca analyze Solana", "handle_analyze", ({"symbol": "Solana"},)),
            ("!dca analyze all", "handle_analyze", ({"symbol": "all"},)),
        )
        for command, handler_name, expected_args in cases:
            with self.subTest(command=command):
                message = MessageStub(content=command)
                with patch.object(
                    discord_bot, handler_name, new_callable=AsyncMock
                ) as handler:
                    handled = asyncio.run(
                        discord_bot._handle_exact_dca_command(command, message)
                    )
                self.assertTrue(handled)
                handler.assert_awaited_once_with(*expected_args, message)

    def test_top_level_read_only_aliases_route_without_gemini(self):
        cases = (
            ("show status", "handle_status"),
            ("SHOW STATUS", "handle_status"),
            ("show portfolio", "handle_portfolio"),
            ("help", "handle_help"),
            ("!help", "handle_help"),
        )
        for command, handler_name in cases:
            with self.subTest(command=command):
                message = MessageStub(content=command)
                with (
                    patch.object(discord_bot, "CHANNEL_ID", "456"),
                    patch.object(discord_bot, "ALLOWED_USERS", "123"),
                    patch.object(
                        discord_bot, handler_name, new_callable=AsyncMock
                    ) as handler,
                    patch.object(
                        discord_bot, "classify_intent", new_callable=AsyncMock
                    ) as classify,
                ):
                    asyncio.run(discord_bot.on_message(message))
                handler.assert_awaited_once_with({}, message)
                classify.assert_not_awaited()

    def test_only_canonical_confirmation_form_reaches_confirmation_handler(self):
        valid = "!dca confirm enable BTC_USD"
        message = MessageStub(content=valid)
        with patch.object(
            discord_bot, "_handle_enable_confirmation", new_callable=AsyncMock
        ) as confirmation:
            self.assertTrue(
                asyncio.run(discord_bot._handle_exact_dca_command(valid, message))
            )
        confirmation.assert_awaited_once_with(message, valid)

        for invalid in (
            "!dca confirmation",
            "!dca confirm enable btc_usd",
            "!dca confirm enable BTC_USD now",
        ):
            with self.subTest(invalid=invalid):
                rejected = MessageStub(content=invalid)
                with patch.object(
                    discord_bot,
                    "_handle_enable_confirmation",
                    new_callable=AsyncMock,
                ) as confirmation:
                    self.assertTrue(
                        asyncio.run(
                            discord_bot._handle_exact_dca_command(invalid, rejected)
                        )
                    )
                confirmation.assert_not_awaited()
                self.assertIn("Command not accepted", rejected.replies[-1])

    def test_intent_allowlist_cannot_authorize_writes_or_analysis(self):
        for action in ("portfolio", "status", "health", "help", "chat", "unknown"):
            with self.subTest(accepted=action):
                self.assertEqual(
                    discord_bot._validate_intent(
                        {"action": action, "params": {}, "reply": "hello"}
                    )["action"],
                    action,
                )
        self.assertEqual(
            discord_bot._validate_intent(
                {
                    "action": "portfolio",
                    "params": {"short_report": False, "workflow": "daily_dca.yml"},
                }
            )["params"],
            {},
        )
        for action in (
            "analyze",
            "set_amounts",
            "set_enabled",
            "enable",
            "disable",
            "confirm",
            "buy",
            "purchase",
            "update_dca",
        ):
            with self.subTest(rejected=action):
                self.assertEqual(
                    discord_bot._validate_intent(
                        {"action": action, "params": {"symbol": "BTC"}}
                    )["action"],
                    "unknown",
                )

    def test_model_prose_is_ignored_and_code_owns_every_chat_reply(self):
        model_replies = (
            "Guaranteed profit — you should buy BTC now 🚀",
            "BTC is certain to double, so enable it before you miss out.",
            "For your goals, increasing the BTC budget is the right move.",
            "Great choice—keep stacking BTC! 🚀",
            "Keep buying BTC; it is going to explode.",
            "BTC will double. Turn it on before the price jumps.",
            "You'll make money if you turn BTC on.",
            "Don't wait—BTC is about to take off.",
            "BTC looks strong 🔥💎.",
            "You can disable a target before changing its budget.",
        )
        expected = discord_bot.CHAT_TOPIC_REPLIES["controls"]
        for model_reply in model_replies:
            with self.subTest(model_reply=model_reply):
                intent = discord_bot._validate_intent(
                    {
                        "action": "chat",
                        "topic": "controls",
                        "reply": model_reply,
                        "params": {"workflow": "daily_dca.yml"},
                    }
                )
                self.assertEqual(intent["reply"], expected)
                self.assertEqual(intent["params"], {})
                self.assertNotIn(model_reply, intent["reply"])

        self.assertIn("do not write a reply", discord_bot.CLASSIFY_PROMPT)
        self.assertIn("Never return reply text", discord_bot.CLASSIFY_PROMPT)
        self.assertFalse(
            any(
                mention in reply
                for reply in discord_bot.CHAT_TOPIC_REPLIES.values()
                for mention in ("@everyone", "@here", "<@")
            )
        )

    def test_chat_history_is_bounded_and_isolated_by_channel_and_user(self):
        first = MessageStub(user_id="123", channel_id="456")
        for index in range(5):
            discord_bot._remember_chat_turn(first, f"user {index}", f"bot {index}")
        first_key = discord_bot._conversation_key(first)
        self.assertEqual(len(discord_bot._chat_histories[first_key]), 3)
        self.assertEqual(discord_bot._chat_histories[first_key][0][0], "user 2")

        second = MessageStub(user_id="999", channel_id="456")
        discord_bot._remember_chat_turn(second, "hello", "hi")
        self.assertNotEqual(
            discord_bot._conversation_key(first),
            discord_bot._conversation_key(second),
        )

        expiring = MessageStub(user_id="555", channel_id="789")
        with patch.object(discord_bot, "monotonic", return_value=100.0):
            discord_bot._remember_chat_turn(expiring, "old", "turn")
        with patch.object(
            discord_bot,
            "monotonic",
            return_value=100.0 + discord_bot.CHAT_HISTORY_TTL_SECONDS + 1,
        ):
            self.assertEqual(discord_bot._recent_chat_history(expiring), [])

    def test_unrelated_activity_globally_prunes_expired_history(self):
        expired = MessageStub(user_id="111", channel_id="456")
        fresh = MessageStub(user_id="222", channel_id="789")
        expired_key = discord_bot._conversation_key(expired)
        with patch.object(discord_bot, "monotonic", return_value=100.0):
            discord_bot._remember_chat_turn(expired, "old", "turn")
        with patch.object(
            discord_bot,
            "monotonic",
            return_value=100.0 + discord_bot.CHAT_HISTORY_TTL_SECONDS + 1,
        ):
            discord_bot._remember_chat_turn(fresh, "new", "turn")
        self.assertNotIn(expired_key, discord_bot._chat_histories)
        self.assertNotIn(expired_key, discord_bot._chat_history_updated_at)

    def test_chat_history_has_a_hard_session_cap(self):
        for index in range(discord_bot.MAX_CHAT_SESSIONS + 5):
            message = MessageStub(user_id=str(index), channel_id="456")
            with patch.object(discord_bot, "monotonic", return_value=100.0 + index):
                discord_bot._remember_chat_turn(message, "hello", "hi")
        self.assertEqual(
            len(discord_bot._chat_histories), discord_bot.MAX_CHAT_SESSIONS
        )
        self.assertEqual(
            len(discord_bot._chat_history_updated_at),
            discord_bot.MAX_CHAT_SESSIONS,
        )
        self.assertNotIn(("456", "0"), discord_bot._chat_histories)

    def test_command_like_near_misses_never_reach_gemini(self):
        commands = (
            " !dca disable BTC",
            "!dca disable BTC ",
            "!DCA disable BTC",
            "!dca  disable BTC",
            "`!dca disable BTC`",
            "```!dca disable BTC```",
            '"!dca disable BTC"',
            "- !dca disable BTC",
            "* !dca enable BTC",
            "please !dca analyze all",
            "```text\n!dca disable BTC\n```",
        )
        for command in commands:
            with self.subTest(command=command):
                message = MessageStub(content=command)
                with (
                    patch.object(discord_bot, "CHANNEL_ID", "456"),
                    patch.object(discord_bot, "ALLOWED_USERS", "123"),
                    patch.object(
                        discord_bot, "classify_intent", new_callable=AsyncMock
                    ) as classify,
                    patch.object(discord_bot, "trigger_workflow") as dispatch,
                ):
                    asyncio.run(discord_bot.on_message(message))
                classify.assert_not_awaited()
                dispatch.assert_not_called()
                self.assertIn("Command not accepted", message.replies[-1])

    def test_natural_chat_is_reply_only_with_mentions_disabled(self):
        message = MessageStub(content="Tell me what DCA means")
        intent = {
            "action": "chat",
            "params": {},
            "reply": "Patient DCA spreads purchases over time.",
        }
        with (
            patch.object(discord_bot, "CHANNEL_ID", "456"),
            patch.object(discord_bot, "ALLOWED_USERS", "123"),
            patch.object(
                discord_bot,
                "classify_intent",
                new_callable=AsyncMock,
                return_value=intent,
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(discord_bot.on_message(message))
        dispatch.assert_not_called()
        self.assertTrue(message.replies[-1].startswith("🐙 "))
        self.assertIn("allowed_mentions", message.reply_kwargs[-1])

    def test_even_malicious_model_action_has_no_write_handler(self):
        message = MessageStub(content="Please enable BTC")
        intent = {"action": "set_enabled", "params": {"symbol": "BTC"}, "reply": ""}
        with (
            patch.object(discord_bot, "CHANNEL_ID", "456"),
            patch.object(discord_bot, "ALLOWED_USERS", "123"),
            patch.object(
                discord_bot,
                "classify_intent",
                new_callable=AsyncMock,
                return_value=intent,
            ),
            patch.object(discord_bot, "handle_enable", new_callable=AsyncMock) as enable,
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(discord_bot.on_message(message))
        enable.assert_not_awaited()
        dispatch.assert_not_called()
        self.assertIn("help", message.replies[-1])

    def test_read_only_keyword_fallback_works_without_gemini_key(self):
        with (
            patch.object(discord_bot, "GEMINI_API_KEY", ""),
            patch.object(discord_bot.genai, "Client") as client,
        ):
            result = asyncio.run(
                discord_bot.classify_intent("Is the scheduler healthy?")
            )
        self.assertEqual(result["action"], "health")
        client.assert_not_called()


class DiscordBotSchedulerTests(unittest.TestCase):
    def setUp(self):
        discord_bot._dca_schedule.clear()
        discord_bot._pending_recovery_symbols.clear()
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
            {"action": "status", "topic": "capabilities"}
        )
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        client.aio.aclose = AsyncMock()
        with (
            patch.object(discord_bot, "GEMINI_API_KEY", "test-key"),
            patch.object(discord_bot.genai, "Client", return_value=client) as ctor,
        ):
            result = asyncio.run(discord_bot.classify_intent("show status"))
        ctor.assert_called_once()
        self.assertEqual(ctor.call_args.kwargs["api_key"], "test-key")
        http_options = ctor.call_args.kwargs["http_options"]
        self.assertEqual(
            http_options.timeout, discord_bot.GEMINI_TIMEOUT_SECONDS * 1_000
        )
        self.assertEqual(http_options.retry_options.attempts, 1)
        self.assertIsInstance(
            http_options.async_client_args["transport"],
            discord_bot.httpx.AsyncHTTPTransport,
        )
        client.aio.models.generate_content.assert_awaited_once_with(
            model="gemini-3.5-flash-lite", contents=ANY, config=ANY
        )
        client.aio.aclose.assert_awaited_once()
        self.assertEqual(result["action"], "status")

    def test_gemini_chat_topic_uses_code_owned_reply(self):
        response = MagicMock()
        response.text = json.dumps(
            {
                "action": "chat",
                "topic": "risk",
                "reply": "Guaranteed profit — you should buy BTC now 🚀 @everyone",
            }
        )
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        client.aio.aclose = AsyncMock()
        with (
            patch.object(discord_bot, "GEMINI_API_KEY", "test-key"),
            patch.object(discord_bot.genai, "Client", return_value=client),
        ):
            result = asyncio.run(discord_bot.classify_intent("Should I buy BTC?"))
        self.assertEqual(result["action"], "chat")
        self.assertEqual(result["reply"], discord_bot.CHAT_TOPIC_REPLIES["risk"])
        self.assertNotIn("Guaranteed profit", result["reply"])
        self.assertNotIn("@everyone", result["reply"])
        client.aio.aclose.assert_awaited_once()

    def test_gemini_timeout_cancels_primary_before_safe_fallback(self):
        cancelled = {"value": False}

        async def slow_request(*args, **kwargs):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        response = MagicMock()
        response.text = json.dumps(
            {"action": "chat", "topic": "dca"}
        )
        primary = MagicMock()
        primary.aio.models.generate_content = AsyncMock(side_effect=slow_request)
        primary.aio.aclose = AsyncMock()
        fallback = MagicMock()
        fallback.aio.models.generate_content = AsyncMock(return_value=response)
        fallback.aio.aclose = AsyncMock()

        with (
            patch.object(discord_bot, "GEMINI_API_KEY", "test-key"),
            patch.object(discord_bot, "GEMINI_TIMEOUT_SECONDS", 0.01),
            patch.object(
                discord_bot.genai,
                "Client",
                side_effect=(primary, fallback),
            ) as ctor,
        ):
            result = asyncio.run(discord_bot.classify_intent("Explain DCA calmly"))

        self.assertTrue(cancelled["value"])
        self.assertEqual(result["action"], "chat")
        self.assertEqual(ctor.call_count, 2)
        primary.aio.aclose.assert_awaited_once()
        fallback.aio.models.generate_content.assert_awaited_once()
        fallback.aio.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

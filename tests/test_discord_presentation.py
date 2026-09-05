"""Offline production-like Discord rendering and access-control regressions."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
import discord_bot as bot
from tests.test_discord_bot import (
    FrozenDateTime, MessageStub, NOW, analysis_state, rules, variable_reader,
)


MATCHED_SHADOW = {"status": "MATCHED", "github_mode": "shadow", "canary_symbol": None}
MATCHED_LIVE = {"status": "MATCHED", "github_mode": "live", "canary_symbol": None}


class DiscordPresentationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "ALLOWED_USERS": "123", "CHANNEL_ID": "456", "DCA_CRON_ENABLED": False,
            "DCA_TRADING_MODE": "shadow", "_schedule_error": None,
            "_schedule_warning": None, "_schedule_start_date": None,
            "GITHUB_REPO": "aesscialo-bot/DCA-Bot",
        }.items():
            self.stack.enter_context(patch.object(bot, name, value))
        FrozenDateTime.current = NOW
        self.stack.enter_context(patch.object(bot, "datetime", FrozenDateTime))
        self.stack.enter_context(patch.object(bot.client, "is_ready", return_value=True))
        self.stack.enter_context(patch.object(bot, "get_analysis_workflow_health", return_value={
            "status": "HEALTHY", "configured_ref": "main", "actual_ref": "main",
            "head_sha": "a" * 40, "run_status": "completed", "conclusion": "success",
            "updated_at": NOW.isoformat(), "reason": None,
        }))
        self.stack.enter_context(patch.object(bot, "get_ghostfolio_delivery_health", return_value={
            "status": "CLEAR", "completed": 4, "pending": 0,
        }))
        bot._pending_enable_confirmations.clear()

    def render(self, *, live_rules=None, decisions=None, execution=None, modes=None, health=False):
        live_rules = live_rules or rules()
        decisions = decisions or analysis_state(live_rules)
        message = MessageStub()
        with (
            patch.object(bot, "_load_live_state", return_value=(live_rules, decisions, execution or {})),
            patch.object(bot, "get_trading_mode_health", return_value=modes or MATCHED_SHADOW),
        ):
            asyncio.run((bot.handle_health if health else bot.handle_status)({}, message))
        return message

    def test_status_is_summary_then_four_distinct_cards_without_truncation(self):
        message = self.render()
        self.assertEqual(len(message.replies), 5)
        self.assertEqual(len(message.embeds), 5)
        self.assertTrue(all(embed.color.value == 0xF0B232 for embed in message.embeds))
        self.assertIn("DCA status", message.replies[0])
        for symbol, part in zip(bot.ALLOWED_TARGETS, message.replies[1:]):
            self.assertIn(symbol.replace("_", "/"), part)
            self.assertIn("Coverage through:", part)
            self.assertIn("Last traded candle:", part)
        self.assertIn("INTENTIONALLY PAUSED", message.text)
        self.assertNotIn("readiness needs attention", message.text.lower())
        self.assertIn("GitHub **SHADOW** (order authority)", message.text)
        self.assertIn("Railway **SHADOW**", message.text)
        self.assertIn("scheduler: **paused", message.text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 1_900 for part in message.replies))

    def test_longest_override_warning_and_final_doge_survive_lossless_parts(self):
        decisions = analysis_state(rules())
        reason = "*review* @everyone <@123> " + "🐕" * 2_500 + " WARNING-END"
        decisions["TARGETS"]["BTC_GBP"]["SIGNALS"].update({
            "UPTREND_OVERRIDE_ACTIVE": True, "UPTREND_OVERRIDE_REASON": reason,
        })
        message = self.render(decisions=decisions)
        self.assertIn("WARNING-END", message.text)
        self.assertIn("DOGE/GBP", message.replies[-1])
        self.assertNotIn("@everyone", message.text)
        self.assertIn("\\*review\\*", message.text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 1_900 for part in message.replies))

    def test_unicode_partitioning_roundtrips_without_losing_warning_characters(self):
        content = "header\n" + "⚠️🐕" * 2_500 + "\nLAST WARNING\n"
        parts = bot._message_parts(content)
        self.assertEqual("".join(parts), content)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 1_900 for part in parts))

    def test_safe_pause_does_not_hide_doge_analysis_failure(self):
        decisions = analysis_state(rules(), status_overrides={"DOGE_GBP": "ERROR"})
        message = self.render(decisions=decisions)
        self.assertIn("INTENTIONALLY PAUSED", message.text)
        self.assertIn("Readiness needs attention", message.text)
        self.assertIn("All-four history gate: **BLOCKED**", message.text)
        self.assertIn("Analysis issue: test analysis error", message.replies[-1])
        health = self.render(decisions=decisions, health=True)
        self.assertIn("INTENTIONALLY PAUSED — ATTENTION REQUIRED", health.text)

    def test_elapsed_same_day_windows_are_not_reported_as_broken_analysis(self):
        decisions = analysis_state(rules(), execute_offsets={target: -90 for target in bot.ALLOWED_TARGETS})
        message = self.render(decisions=decisions, health=True)
        self.assertIn("INTENTIONALLY PAUSED", message.text)
        self.assertNotIn("ATTENTION REQUIRED", message.text)
        self.assertIn("Execution windows closed for today", message.text)
        self.assertIn("no late purchases will be replayed", message.text)

    def test_validation_failure_cannot_overflow_the_most_important_warning(self):
        for handler in (bot.handle_status, bot.handle_health):
            message = MessageStub()
            with patch.object(bot, "_load_live_state", side_effect=bot.ConfigError("bad " * 1800 + "END-MARKER")):
                asyncio.run(handler({}, message))
            self.assertIn("NOT READY", message.replies[0])
            self.assertIn("END-MARKER", message.text)
            self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 1900 for part in message.replies))
            self.assertTrue(all(embed.color.value == 0xED4245 for embed in message.embeds))

    def test_unconfirmed_dispatch_never_claims_no_change_or_encourages_blind_retry(self):
        live_rules = rules()
        message = MessageStub()
        with (
            patch.object(bot, "get_repo_variable", side_effect=variable_reader(live_rules, analysis_state(live_rules))),
            patch.object(bot, "trigger_workflow", return_value=False),
        ):
            asyncio.run(bot.handle_disable("DOGE", message))
            asyncio.run(bot.handle_enable("DOGE", message))
            asyncio.run(bot._handle_enable_confirmation(message, "!dca confirm enable DOGE_GBP"))
            asyncio.run(bot.handle_analyze({"symbol": "all"}, message))
        self.assertEqual(message.text.count("Dispatch not confirmed"), 3)
        self.assertEqual(message.text.count("may already have accepted"), 3)
        self.assertNotIn("The target remains disabled", message.text)
        self.assertNotIn("No rules were changed", message.text)
        self.assertNotIn("Existing decisions will not be reused", message.text)

    def test_zero_doge_budgets_explain_why_enable_is_unavailable(self):
        live_rules = rules()
        live_rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 0, "MID": 0, "UP": 0}
        message = self.render(live_rules=live_rules)
        self.assertIn("£0 cannot be enabled", message.replies[-1])
        self.assertIn("approve and set LOW / MID / UP budgets", message.replies[-1])

    def test_mismatched_and_unknown_modes_never_present_buying_as_ready(self):
        live_rules = rules(enabled=bot.ALLOWED_TARGETS)
        for status, github_mode in (("UNKNOWN", "unknown"), ("MISMATCH", "live")):
            with self.subTest(status=status):
                modes = {"status": status, "github_mode": github_mode}
                message = self.render(live_rules=live_rules, modes=modes)
                self.assertIn("buying readiness unverified", message.text)
                self.assertEqual(message.text.count("Orders: **⚠️ UNVERIFIED"), 4)
                self.assertNotIn("LIVE ORDERS ALLOWED", message.text)
                self.assertIn("DCA health: ATTENTION REQUIRED", self.render(live_rules=live_rules, modes=modes, health=True).text)

    def test_disabled_doge_error_blocks_enabled_bitcoin_globally(self):
        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(live_rules, status_overrides={"DOGE_GBP": "ERROR"})
        message = self.render(live_rules=live_rules, decisions=decisions, modes=MATCHED_LIVE)
        self.assertIn("all four histories need current analysis", message.replies[1])

    def test_pending_recovery_and_already_bought_have_explicit_order_postures(self):
        live_rules = rules(enabled=bot.ALLOWED_TARGETS)
        for execution, expected in (({"DOGE_GBP": {"PENDING_ORDER": {}}}, "RECOVERY ONLY"),
                                    ({"BTC_GBP": {"LAST_BUY_DATE": NOW.astimezone(bot.TIMEZONE).date().isoformat()}}, "ALREADY BOUGHT TODAY")):
            message = self.render(live_rules=live_rules, execution=execution, modes=MATCHED_LIVE)
            self.assertIn(expected, message.replies[1])

    def test_live_mode_still_distinguishes_waiting_due_and_not_authorized_here(self):
        live_rules = rules(enabled=bot.ALLOWED_TARGETS)
        decisions = analysis_state(live_rules)
        message = self.render(live_rules=live_rules, decisions=decisions, modes=MATCHED_LIVE)
        self.assertIn("WAITING FOR EXECUTION WINDOW", message.replies[1])
        decisions = analysis_state(live_rules, execute_offsets={target: 0 for target in bot.ALLOWED_TARGETS})
        message = self.render(live_rules=live_rules, decisions=decisions, modes=MATCHED_LIVE)
        self.assertIn("trader must recheck quote, minimum, balance and live state", message.replies[1])

    def test_changed_enabled_snapshot_requires_post_enable_analysis(self):
        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(live_rules)
        decisions["TARGETS"]["BTC_GBP"]["ENABLED"] = False
        message = self.render(live_rules=live_rules, decisions=decisions, modes=MATCHED_LIVE)
        self.assertIn("WAITING FOR FRESH ANALYSIS", message.replies[1])

    def test_private_handlers_fail_closed_for_every_authorization_failure(self):
        for handler in (bot.handle_status, bot.handle_health, bot.handle_portfolio):
            for failure in ("empty_allowlist", "missing_channel", "wrong_channel", "wrong_user", "bot_user"):
                with self.subTest(handler=handler.__name__, failure=failure):
                    message = MessageStub(user_id="999" if failure == "wrong_user" else "123")
                    message.author.bot = failure == "bot_user"
                    if failure == "wrong_channel":
                        message.channel.id = "789"
                    with (
                        patch.object(bot, "ALLOWED_USERS", "" if failure == "empty_allowlist" else "123"),
                        patch.object(bot, "CHANNEL_ID", None if failure == "missing_channel" else "456"),
                        patch.object(bot, "_load_live_state") as read,
                        patch.object(bot, "trigger_workflow") as dispatch,
                    ):
                        asyncio.run(handler({}, message))
                    read.assert_not_called()
                    dispatch.assert_not_called()
                    self.assertIn("Blocked:", message.text)

    def test_missing_allowlist_never_calls_gemini_or_read_dispatch(self):
        message = MessageStub()
        message.content = "show portfolio"
        message.mentions = []
        with (patch.object(bot, "ALLOWED_USERS", ""), patch.object(bot, "classify_intent") as ai,
              patch.object(bot, "trigger_workflow") as dispatch):
            asyncio.run(bot.on_message(message))
        ai.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(message.replies, [])

    def test_mentions_disabled_globally_and_on_split_messages(self):
        self.assertEqual(bot.client.allowed_mentions.to_dict(), discord.AllowedMentions.none().to_dict())
        message = SimpleNamespace(reply=AsyncMock())
        asyncio.run(bot._reply_sections(message, ["hello @everyone"]))
        options = message.reply.await_args.kwargs["allowed_mentions"]
        self.assertEqual(options.to_dict(), discord.AllowedMentions.none().to_dict())

    def test_external_error_text_masks_known_secrets_and_escapes_markdown(self):
        with patch.dict("os.environ", {"GH_PAT": "private-token-value"}):
            text = bot._safe_text("private-token-value **fake ready** @everyone")
        self.assertNotIn("private-token-value", text)
        self.assertNotIn("@everyone", text)
        self.assertIn("\\*\\*fake ready\\*\\*", text)

    def test_config_ack_contains_workflow_link_without_claiming_applied(self):
        message = MessageStub()
        with patch.object(bot, "trigger_workflow", return_value=True):
            asyncio.run(bot.handle_disable("DOGE", message))
        self.assertIn("Queued disable", message.text)
        self.assertIn("https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/update_dca_config.yml", message.text)
        self.assertNotIn("**Applied", message.text)

    def test_confirmation_expiry_replay_and_changed_budgets_never_dispatch(self):
        for failure in ("expired", "replay", "changed_budget"):
            with self.subTest(failure=failure):
                bot._pending_enable_confirmations.clear()
                live_rules = rules()
                reader = variable_reader(live_rules, analysis_state(live_rules))
                message = MessageStub()
                with patch.object(bot, "get_repo_variable", side_effect=reader):
                    asyncio.run(bot.handle_enable("DOGE", message))
                if failure == "expired":
                    bot._pending_enable_confirmations["123"]["expires_at"] = 0
                elif failure == "replay":
                    bot._pending_enable_confirmations.clear()
                else:
                    live_rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 11
                with (patch.object(bot, "get_repo_variable", side_effect=variable_reader(live_rules, analysis_state(live_rules))),
                      patch.object(bot, "trigger_workflow") as dispatch):
                    asyncio.run(bot._handle_enable_confirmation(message, "!dca confirm enable DOGE_GBP"))
                dispatch.assert_not_called()
                self.assertIn("Blocked:", message.replies[-1])

    def test_modes_read_authoritative_github_values_not_railway_assumptions(self):
        for raw, expected in ((None, "UNKNOWN"), ("invalid", "UNKNOWN"), ("live", "MISMATCH"), ("shadow", "MATCHED")):
            with patch.object(bot, "get_repo_variable", return_value=raw):
                result = bot.get_trading_mode_health()
            self.assertEqual(result["status"], expected)
        with (patch.object(bot, "DCA_TRADING_MODE", "canary"),
              patch.object(bot, "get_repo_variable", side_effect=["canary", "DOGE_GBP"])):
            self.assertEqual(bot.get_trading_mode_health()["status"], "MISMATCH")


if __name__ == "__main__":
    unittest.main()

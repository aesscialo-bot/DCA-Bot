"""Offline bulk-control and disable-versus-review race regressions."""

import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import discord_bot as bot
from dca_config import ALLOWED_TARGETS
from tests.test_discord_bot import MessageStub, rules


class DiscordBulkControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.live_rules = rules()
        self.execution = {}
        self.now = 100.0
        self.message = MessageStub()
        bot._pending_enable_confirmations.clear()
        bot._enable_review_revisions.update({target: 0 for target in ALLOWED_TARGETS})
        for name, value in (
            ("ALLOWED_USERS", "123,789,987"),
            ("CHANNEL_ID", "456"),
            ("GITHUB_REPO", "example/dca"),
            ("DCA_TRADING_MODE", "shadow"),
            ("DCA_CRON_ENABLED", False),
        ):
            patched = patch.object(bot, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        reader_patch = patch.object(bot, "get_repo_variable", side_effect=self.read_variable)
        self.reader = reader_patch.start()
        self.addCleanup(reader_patch.stop)
        dispatch_patch = patch.object(bot, "trigger_workflow", return_value=True)
        self.dispatch = dispatch_patch.start()
        self.addCleanup(dispatch_patch.stop)
        time_patch = patch.object(bot, "monotonic", side_effect=lambda: self.now)
        time_patch.start()
        self.addCleanup(time_patch.stop)

    def tearDown(self):
        bot._pending_enable_confirmations.clear()
        bot._enable_review_revisions.update({target: 0 for target in ALLOWED_TARGETS})

    def read_variable(self, name):
        if name == bot.RULES_VARIABLE:
            return json.dumps(self.live_rules)
        if name == bot.EXECUTION_STATE_VARIABLE:
            return json.dumps(self.execution)
        raise AssertionError(f"Unexpected variable read: {name}")

    def add_pending_order(self, target="DOGE_GBP"):
        self.execution[target] = {
            "LAST_BUY_DATE": "",
            "PENDING_ORDER": {
                "client_order_id": "dca-1234567890abcd",
                "funding_client_order_id": "dca-fedcba09876543",
                "trade_date": "2026-08-05",
                "amount_gbp": 10,
                "decision_id": "decision-doge_gbp",
                "created_at": "2026-08-05T03:55:00Z",
            },
        }

    async def review(self, symbol="all", message=None):
        await bot.handle_enable(symbol, message or self.message)

    async def confirm(self, text="!dca confirm enable all", message=None):
        await bot._handle_enable_confirmation(message or self.message, text)

    async def test_mixed_enable_review_shows_all_budgets_flags_and_total(self):
        self.live_rules["BTC_GBP"]["BUY_ENABLED"] = True
        self.live_rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 5, "MID": 10, "UP": 15}
        before = deepcopy(self.live_rules)
        await self.review()
        self.dispatch.assert_not_called()
        for target in ALLOWED_TARGETS:
            self.assertIn(target.replace("_", "/"), self.message.text)
        self.assertIn("BTC/GBP** — currently ENABLED", self.message.text)
        self.assertIn("DOGE/GBP** — currently DISABLED", self.message.text)
        self.assertIn("£5 / £10 / £15", self.message.text)
        self.assertIn("£10 / £15 / £20", self.message.text)
        self.assertIn("**£75**", self.message.text)
        self.assertIn("!dca confirm enable all", self.message.text)
        self.assertIn("within 5 minutes", self.message.text)
        self.assertIn("next successful analysis", self.message.text)
        self.assertIn("does not change trading modes or scheduling and places no immediate order", self.message.text)
        self.assertEqual(self.live_rules, before)
        self.assertTrue(self.message.embeds)
        for embed in self.message.embeds:
            self.assertLessEqual(len(embed.description.encode("utf-16-le")) // 2, 4096)

    async def test_exact_bulk_confirmation_dispatches_once_with_two_global_hashes(self):
        self.live_rules["BTC_GBP"]["BUY_ENABLED"] = True
        expected_hash = bot.global_rules_pre_state_hash(self.live_rules)
        await self.review()
        await self.confirm()
        self.dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled", "symbol": "all", "enabled_json": "true",
                "expected_rules_hash": expected_hash,
                "expected_global_rules_hash": expected_hash,
            },
        )
        self.assertNotIn("123", bot._pending_enable_confirmations)
        self.assertIn("No configuration result is confirmed yet", self.message.text)
        self.assertIn("reports APPLIED, run `!dca analyze all`", self.message.text)
        self.assertIn("https://github.com/example/dca/actions/workflows/update_dca_config.yml", self.message.text)
        self.assertEqual(bot.DCA_TRADING_MODE, "shadow")
        self.assertFalse(bot.DCA_CRON_ENABLED)

    async def test_all_enabled_is_clear_noop(self):
        self.live_rules = rules(enabled=ALLOWED_TARGETS)
        await self.review()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("All four targets are already enabled. No workflow queued", self.message.text)

    async def test_one_zero_budget_blocks_entire_enable(self):
        self.live_rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 0, "MID": 0, "UP": 0}
        await self.review()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("DOGE\\_GBP LOW must be between £5", self.message.text)

    async def test_already_enabled_target_still_needs_valid_budget_in_mixed_review(self):
        self.live_rules["BTC_GBP"]["BUY_ENABLED"] = True
        self.live_rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 1, "MID": 2, "UP": 3}
        await self.review()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("BTC\\_GBP.LOW must be £0 while unconfigured or at least £5", self.message.text)

    async def test_malformed_rules_fail_closed(self):
        self.live_rules.pop("DOGE_GBP")
        await self.review()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("Blocked", self.message.text)

    async def test_pending_order_in_any_target_blocks_whole_review(self):
        for target in ALLOWED_TARGETS:
            with self.subTest(target=target):
                self.execution = {}
                self.add_pending_order(target)
                await self.review()
                self.assertFalse(bot._pending_enable_confirmations)
        self.dispatch.assert_not_called()
        self.assertIn("reconciliation is pending", self.message.text)

    async def test_order_arriving_after_review_blocks_confirmation(self):
        await self.review()
        self.add_pending_order()
        await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("Blocked after live revalidation", self.message.text)

    async def test_budget_change_after_review_blocks_confirmation(self):
        await self.review()
        self.live_rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 11
        await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("global four-asset DCA rules changed", self.message.text)

    async def test_enable_flag_change_after_review_blocks_confirmation(self):
        await self.review()
        self.live_rules["ETH_GBP"]["BUY_ENABLED"] = True
        await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("global four-asset DCA rules changed", self.message.text)

    async def test_confirmation_is_exact_and_bound_to_user(self):
        await self.review()
        await self.confirm("!dca confirm enable ALL")
        await self.confirm("!dca confirm enable BTC_GBP")
        other = MessageStub("789")
        await self.confirm(message=other)
        self.dispatch.assert_not_called()
        self.assertIn("send exactly", self.message.text)
        self.assertIn("no enable confirmation is pending for your user", other.text)
        self.assertIn("123", bot._pending_enable_confirmations)

    async def test_confirmation_expires_at_five_minutes(self):
        await self.review()
        self.now += 300
        await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("expired", self.message.text)

    async def test_confirmation_expiry_is_rechecked_after_live_reads(self):
        await self.review()

        async def delayed_read(function, *args):
            self.now += 150
            return function(*args)

        with patch.object(bot.asyncio, "to_thread", side_effect=delayed_read):
            await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("expired during validation", self.message.text)

    async def test_confirmed_or_ambiguous_dispatch_cannot_replay(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                self.dispatch.reset_mock()
                self.dispatch.return_value = accepted
                await self.review()
                await self.confirm()
                await self.confirm()
                self.dispatch.assert_called_once()
                self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("may already have accepted it", self.message.text)

    async def test_bulk_disable_is_immediate_single_dispatch_without_reads(self):
        await bot.handle_disable("all", self.message)
        self.reader.assert_not_called()
        self.dispatch.assert_called_once_with(
            "update_dca_config.yml", {"action": "set_enabled", "symbol": "all", "enabled_json": "false"},
        )
        self.assertIn("Queued disable for **all four targets**", self.message.text)
        self.assertIn("order already accepted by Kraken will still be reconciled", self.message.text)
        self.assertEqual(bot.DCA_TRADING_MODE, "shadow")
        self.assertFalse(bot.DCA_CRON_ENABLED)

    async def test_disable_all_cancels_every_user_before_dispatch_even_if_ambiguous(self):
        await self.review()
        await self.review("BTC", MessageStub("789"))
        await self.review("ETH", MessageStub("987"))
        self.assertEqual(len(bot._pending_enable_confirmations), 3)

        async def assert_cancelled_before_await(function, *args):
            self.assertIs(function, bot.trigger_workflow)
            self.assertFalse(bot._pending_enable_confirmations)
            self.assertEqual(set(bot._enable_review_revisions.values()), {1})
            return False

        with patch.object(bot.asyncio, "to_thread", side_effect=assert_cancelled_before_await):
            await bot.handle_disable("all", self.message)
        await self.confirm()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertIn("cancelled locally", self.message.text)
        self.assertIn("may already have accepted it", self.message.text)

    async def test_single_disable_cancels_overlapping_reviews_for_all_users_only(self):
        await self.review()
        await self.review("BTC", MessageStub("789"))
        await self.review("ETH", MessageStub("987"))
        await bot.handle_disable("BTC", self.message)
        self.assertEqual(set(bot._pending_enable_confirmations), {"987"})
        self.assertEqual(bot._enable_review_revisions["BTC_GBP"], 1)
        self.assertEqual(bot._enable_review_revisions["ETH_GBP"], 0)

    async def assert_disable_wins_during_reads(self, *, confirmation, review_symbol="all", disable_symbol="all"):
        if confirmation:
            await self.review(review_symbol)
        reads_started = asyncio.Event()
        release_reads = asyncio.Event()

        async def delayed_read(function, *args):
            if function is bot.get_repo_variable:
                reads_started.set()
                await release_reads.wait()
            return function(*args)

        with patch.object(bot.asyncio, "to_thread", side_effect=delayed_read):
            if confirmation:
                command = bot._pending_enable_confirmations["123"]["command"]
                pending_task = asyncio.create_task(self.confirm(command))
            else:
                pending_task = asyncio.create_task(self.review(review_symbol))
            await asyncio.wait_for(reads_started.wait(), timeout=2)
            await bot.handle_disable(disable_symbol, MessageStub("789"))
            release_reads.set()
            await asyncio.wait_for(pending_task, timeout=2)
        self.assertFalse(bot._pending_enable_confirmations)
        self.dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "action": "set_enabled",
                "symbol": "all" if disable_symbol == "all" else f"{disable_symbol}_GBP",
                "enabled_json": "false",
            },
        )

    async def test_disable_all_cancels_bulk_review_whose_reads_are_in_flight(self):
        await self.assert_disable_wins_during_reads(confirmation=False)
        self.assertIn("disable was requested during this review", self.message.text)

    async def test_disable_all_cancels_bulk_confirmation_whose_reads_are_in_flight(self):
        await self.assert_disable_wins_during_reads(confirmation=True)
        self.assertIn("replaced or already consumed", self.message.text)

    async def test_disable_all_cancels_inflight_single_review(self):
        await self.assert_disable_wins_during_reads(confirmation=False, review_symbol="BTC")

    async def test_single_disable_cancels_inflight_bulk_confirmation(self):
        await self.assert_disable_wins_during_reads(confirmation=True, disable_symbol="DOGE")

    async def test_duplicate_confirmation_tasks_only_dispatch_once(self):
        await self.review()
        await asyncio.gather(self.confirm(), self.confirm())
        self.dispatch.assert_called_once()
        self.assertFalse(bot._pending_enable_confirmations)

    async def test_unauthorized_users_channels_and_empty_allowlist_cannot_use_bulk(self):
        for message, allowlist in (
            (MessageStub("999"), "123"),
            (MessageStub(), ""),
            (MessageStub(), "123"),
        ):
            if allowlist == "123" and message.author.id == "123":
                message.channel.id = "unapproved"
            with patch.object(bot, "ALLOWED_USERS", allowlist):
                await self.review(message=message)
                await bot.handle_disable("all", message)
                await self.confirm(message=message)
            self.assertIn("Blocked", message.text)
        self.reader.assert_not_called()
        self.dispatch.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)
        self.assertEqual(set(bot._enable_review_revisions.values()), {0})

    async def test_exact_command_router_recognizes_bulk_commands(self):
        self.assertTrue(await bot._handle_exact_dca_command("!dca enable all", self.message))
        self.assertTrue(await bot._handle_exact_dca_command("!dca confirm enable all", self.message))
        self.assertTrue(await bot._handle_exact_dca_command("!dca disable all", self.message))
        self.assertEqual(self.dispatch.call_count, 2)
        self.assertEqual([call.args[1]["symbol"] for call in self.dispatch.call_args_list], ["all", "all"])

    async def test_uppercase_all_is_not_an_alias_for_bulk_writes(self):
        await self.review("ALL")
        await bot.handle_disable("ALL", self.message)
        self.dispatch.assert_not_called()
        self.reader.assert_not_called()
        self.assertFalse(bot._pending_enable_confirmations)

    async def test_help_describes_bulk_scope_confirmation_and_paused_modes(self):
        await bot.handle_help({}, self.message)
        for command in ("!dca enable all", "!dca disable all", "!dca confirm enable all", "!dca analyze all"):
            self.assertIn(command, self.message.text)
        self.assertIn("five minutes", self.message.text)
        self.assertIn("do not change modes or scheduling and place no immediate order", self.message.text)
        self.assertIn("Any invalid budget or pending order blocks bulk enable", self.message.text)


if __name__ == "__main__":
    unittest.main()

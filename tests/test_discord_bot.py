import asyncio
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import discord_bot


class MessageStub:
    def __init__(self, user_id="123"):
        self.replies = []
        self.author = SimpleNamespace(id=user_id)

    async def reply(self, content):
        self.replies.append(content)


def rule(*, amount=25, enabled=False, target_time="00:45"):
    return {
        "TIME": target_time,
        "AMOUNT_GBP": amount,
        "BUY_ENABLED": enabled,
        "DYNAMIC_DCA": {"ENABLED": False},
    }


class DiscordBotGbpConfigTests(unittest.TestCase):
    def setUp(self):
        self.allowed_users = patch.object(discord_bot, "ALLOWED_USERS", "123")
        self.allowed_users.start()
        self.addCleanup(self.allowed_users.stop)
        discord_bot._pending_enable_confirmations.clear()
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._pending_recovery_symbols.clear()
        discord_bot._dca_schedule.clear()
        self.target_map = {"BTC_GBP": rule()}

    def test_symbol_normalisation_is_gbp_only(self):
        self.assertEqual(discord_bot._normalise_gbp_key("bitcoin"), "BTC_GBP")
        self.assertEqual(discord_bot._normalise_gbp_key("BTC/GBP"), "BTC_GBP")
        self.assertEqual(discord_bot._to_gbp_pair("link"), "LINK/GBP")
        with self.assertRaisesRegex(ValueError, "Only GBP"):
            discord_bot._normalise_gbp_key("BTC/USD")

    def test_analysis_symbols_are_derived_from_gbp_keys(self):
        raw = json.dumps({**self.target_map, "LINK_GBP": rule()})
        with patch.object(
            discord_bot, "_get_repo_variable_and_refresh", return_value=raw
        ):
            self.assertEqual(
                discord_bot._symbols_from_dca_map(), "BTC/GBP, LINK/GBP"
            )

    def test_analysis_rejects_non_gbp_map_instead_of_falling_back(self):
        raw = json.dumps({"BTC_USD": rule()})
        with patch.object(
            discord_bot, "_get_repo_variable_and_refresh", return_value=raw
        ):
            with self.assertRaisesRegex(ValueError, "non-GBP"):
                discord_bot._symbols_from_dca_map()

    def test_gbp_amount_limits_are_inclusive_and_queued(self):
        for amount in (5, 1000):
            with self.subTest(amount=amount):
                message = MessageStub()
                with (
                    patch.object(
                        discord_bot,
                        "_get_repo_variable_and_refresh",
                        return_value=json.dumps(self.target_map),
                    ),
                    patch.object(
                        discord_bot, "trigger_workflow", return_value=True
                    ) as dispatch,
                ):
                    asyncio.run(
                        discord_bot.handle_update_dca(
                            {"symbol": "BTC", "field": "AMOUNT", "value": amount},
                            message,
                        )
                    )

                dispatch.assert_called_once_with(
                    "update_dca_config.yml",
                    {
                        "symbol": "BTC_GBP",
                        "field": "AMOUNT_GBP",
                        "value_json": json.dumps(amount),
                    },
                )
                self.assertIn("£", message.replies[0])
                self.assertIn("Queued", message.replies[0])

    def test_gbp_amounts_outside_limits_are_rejected(self):
        for amount in (4.99, 1000.01):
            with self.subTest(amount=amount):
                message = MessageStub()
                with patch.object(discord_bot, "trigger_workflow") as dispatch:
                    asyncio.run(
                        discord_bot.handle_update_dca(
                            {
                                "symbol": "BTC",
                                "field": "AMOUNT_GBP",
                                "value": amount,
                            },
                            message,
                        )
                    )
                dispatch.assert_not_called()
                self.assertIn("£5", message.replies[0])

    def test_direct_purchase_action_is_not_supported(self):
        intent = discord_bot._validate_intent(
            {"action": "instant_purchase", "params": {"symbol": "BTC"}}
        )
        self.assertEqual(intent["action"], "unknown")
        self.assertNotIn("instant_purchase", discord_bot.ACTION_HANDLERS)

    def test_zero_placeholder_cannot_be_enabled(self):
        message = MessageStub()
        zero_map = {"BTC_GBP": rule(amount=0, enabled=False)}
        with (
            patch.object(
                discord_bot,
                "_get_repo_variable_and_refresh",
                return_value=json.dumps(zero_map),
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot.handle_update_dca(
                    {"symbol": "BTC", "field": "BUY_ENABLED", "value": True},
                    message,
                )
            )
        dispatch.assert_not_called()
        self.assertIn("Set AMOUNT_GBP", message.replies[0])

    def test_enabling_requires_same_user_exact_second_confirmation(self):
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "_get_repo_variable_and_refresh",
                return_value=json.dumps(self.target_map),
            ),
            patch.object(
                discord_bot, "trigger_workflow", return_value=True
            ) as dispatch,
        ):
            asyncio.run(
                discord_bot.handle_update_dca(
                    {"symbol": "BTC", "field": "BUY_ENABLED", "value": True},
                    message,
                )
            )
            dispatch.assert_not_called()
            expected = "!dca confirm enable BTC_GBP"
            self.assertIn(expected, message.replies[-1])

            asyncio.run(
                discord_bot._handle_enable_confirmation(
                    message, "!dca confirm enable btc_gbp"
                )
            )
            dispatch.assert_not_called()
            self.assertIn("did not match", message.replies[-1])

            wrong_user = MessageStub(user_id="999")
            asyncio.run(discord_bot._handle_enable_confirmation(wrong_user, expected))
            dispatch.assert_not_called()

            asyncio.run(discord_bot._handle_enable_confirmation(message, expected))

        dispatch.assert_called_once_with(
            "update_dca_config.yml",
            {
                "symbol": "BTC_GBP",
                "field": "BUY_ENABLED",
                "value_json": "true",
                "expected_amount_gbp_json": "25.0",
                "expected_time": "00:45",
            },
        )
        self.assertNotIn("123", discord_bot._pending_enable_confirmations)

    def test_enable_confirmation_is_bound_to_amount_and_time_snapshot(self):
        message = MessageStub()
        changed_map = {"BTC_GBP": rule(amount=30, target_time="01:00")}
        with (
            patch.object(
                discord_bot,
                "_get_repo_variable_and_refresh",
                side_effect=[
                    json.dumps(self.target_map),
                    json.dumps(changed_map),
                ],
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot.handle_update_dca(
                    {"symbol": "BTC", "field": "BUY_ENABLED", "value": True},
                    message,
                )
            )
            asyncio.run(
                discord_bot._handle_enable_confirmation(
                    message, "!dca confirm enable BTC_GBP"
                )
            )

        dispatch.assert_not_called()
        self.assertIn("amount or time changed", message.replies[-1])

    def test_status_reads_buy_dates_and_pending_intent_from_execution_state(self):
        message = MessageStub()
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-03",
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "trade_date": "2026-08-04",
                    "amount_gbp": 25,
                },
            }
        }

        def get_variable(name):
            if name == "DCA_TARGET_MAP":
                return json.dumps(self.target_map)
            if name == discord_bot.EXECUTION_STATE_VARIABLE:
                return json.dumps(state)
            return None

        with patch.object(discord_bot, "get_repo_variable", side_effect=get_variable):
            asyncio.run(discord_bot.handle_status({}, message))

        self.assertIn("£25", message.replies[0])
        self.assertIn("2026-08-03", message.replies[0])
        self.assertIn("reconciliation pending", message.replies[0])

    def test_scheduler_rejects_any_non_gbp_key(self):
        raw = json.dumps(
            {
                "BTC_GBP": rule(enabled=True, target_time="01:15"),
                "BTC_USD": rule(enabled=True, target_time="02:15"),
            }
        )
        discord_bot.refresh_dca_schedule(raw, "{}")
        self.assertEqual(discord_bot._dca_schedule, {})

    def test_scheduler_defaults_missing_buy_enabled_to_fail_closed(self):
        discord_bot.refresh_dca_schedule(
            json.dumps(
                {"BTC_GBP": {"TIME": "01:15", "AMOUNT_GBP": 25}}
            ),
            "{}",
        )
        self.assertEqual(discord_bot._dca_schedule, {})

    def test_scheduler_rejects_invalid_amount_or_time(self):
        for invalid_rule in (
            rule(amount="25", enabled=True, target_time="01:15"),
            rule(amount=25, enabled=True, target_time="25:00"),
        ):
            with self.subTest(rule=invalid_rule):
                discord_bot.refresh_dca_schedule(
                    json.dumps({"BTC_GBP": invalid_rule}), "{}"
                )
                self.assertEqual(discord_bot._dca_schedule, {})

    def test_late_night_target_is_dispatched_before_midnight_not_after(self):
        rules = {
            "BTC_GBP": rule(enabled=True, target_time="23:59")
        }
        discord_bot.refresh_dca_schedule(json.dumps(rules), "{}")
        before_midnight = datetime(
            2026, 8, 4, 23, 55, tzinfo=discord_bot.TIMEZONE
        )
        after_midnight = datetime(
            2026, 8, 5, 0, 0, tzinfo=discord_bot.TIMEZONE
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(before_midnight), ["BTC_GBP"]
        )
        self.assertEqual(discord_bot._due_symbols_for_dispatch(after_midnight), [])

    def test_scheduler_uses_execution_state_and_temporary_dispatch_guard(self):
        now = datetime(2026, 8, 4, 1, 15, tzinfo=discord_bot.TIMEZONE)
        today = now.strftime("%Y-%m-%d")
        rules = {
            "BTC_GBP": rule(enabled=True, target_time="01:15"),
            "ETH_GBP": rule(enabled=True, target_time="01:15"),
        }
        state = {"ETH_GBP": {"LAST_BUY_DATE": today}}
        discord_bot.refresh_dca_schedule(json.dumps(rules), json.dumps(state))

        self.assertEqual(discord_bot._due_symbols_for_dispatch(now), ["BTC_GBP"])
        discord_bot._dca_dispatch_guard[("BTC_GBP", today)] = discord_bot.monotonic()
        self.assertEqual(discord_bot._due_symbols_for_dispatch(now), [])

    def test_scheduler_retries_after_dispatch_cooldown(self):
        now = datetime(2026, 8, 4, 1, 15, tzinfo=discord_bot.TIMEZONE)
        today = now.strftime("%Y-%m-%d")
        rules = {"BTC_GBP": rule(enabled=True, target_time="01:15")}
        discord_bot.refresh_dca_schedule(json.dumps(rules), "{}")
        discord_bot._dca_dispatch_guard[("BTC_GBP", today)] = 10.0

        with patch.object(
            discord_bot,
            "monotonic",
            return_value=10.0 + discord_bot.DISPATCH_RETRY_SECONDS + 1,
        ):
            due = discord_bot._due_symbols_for_dispatch(now)

        self.assertEqual(due, ["BTC_GBP"])
        self.assertNotIn(("BTC_GBP", today), discord_bot._dca_dispatch_guard)

    def test_pending_intent_is_reconciled_even_when_rule_disabled(self):
        now = datetime(2026, 8, 4, 1, 15, tzinfo=discord_bot.TIMEZONE)
        state = {
            "BTC_GBP": {
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "trade_date": "2026-08-03",
                    "amount_gbp": 25,
                }
            }
        }
        discord_bot.refresh_dca_schedule(
            json.dumps({"BTC_GBP": rule(amount=0, enabled=False)}),
            json.dumps(state),
        )
        self.assertEqual(discord_bot._due_symbols_for_dispatch(now), ["BTC_GBP"])

    def test_successful_scheduler_dispatch_sets_temporary_guard(self):
        now = datetime(2026, 8, 4, 1, 15, tzinfo=discord_bot.TIMEZONE)
        discord_bot._dca_schedule["01:15"] = {"symbols": {"BTC_GBP": ""}}
        with (
            patch.object(discord_bot, "datetime") as datetime_mock,
            patch.object(
                discord_bot, "_due_symbols_for_dispatch", return_value=["BTC_GBP"]
            ),
            patch.object(
                discord_bot, "trigger_workflow", return_value=True
            ) as dispatch,
        ):
            datetime_mock.now.return_value = now
            asyncio.run(discord_bot.dca_scheduler_tick.coro())

        dispatch.assert_called_once_with("daily_dca.yml")
        self.assertIn(("BTC_GBP", "2026-08-04"), discord_bot._dca_dispatch_guard)


class DiscordBotWriteSafetyTests(unittest.TestCase):
    def test_config_writes_require_allowlist_even_for_dm_author(self):
        message = MessageStub()
        with patch.object(discord_bot, "ALLOWED_USERS", ""):
            reason = discord_bot._config_write_block_reason(
                "update_dca", "!dca disable BTC", message
            )
        self.assertIn("DISCORD_ALLOWED_USERS", reason)

    def test_config_writes_require_exact_case_sensitive_prefix(self):
        message = MessageStub()
        with patch.object(discord_bot, "ALLOWED_USERS", "123"):
            self.assertIsNone(
                discord_bot._config_write_block_reason(
                    "update_dca", "!dca disable BTC", message
                )
            )
            for text in ("disable BTC", "!DCA disable BTC", " !dca disable BTC"):
                with self.subTest(text=text):
                    reason = discord_bot._config_write_block_reason(
                        "update_dca", text, message
                    )
                    self.assertIn("start exactly", reason)


class DiscordBotWorkflowDispatchTests(unittest.TestCase):
    @patch.object(discord_bot.requests, "post")
    def test_dispatch_uses_configured_workflow_ref_and_inputs(self, post):
        post.return_value.status_code = 204
        with patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "codex/kraken-gbp"):
            result = discord_bot.trigger_workflow(
                "update_dca_config.yml", {"symbol": "BTC_GBP"}
            )
        self.assertTrue(result)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"ref": "codex/kraken-gbp", "inputs": {"symbol": "BTC_GBP"}},
        )

    @patch.object(discord_bot.requests, "post")
    def test_dispatch_fails_closed_without_workflow_ref(self, post):
        with patch.object(discord_bot, "GITHUB_WORKFLOW_REF", ""):
            result = discord_bot.trigger_workflow("daily_dca.yml")
        self.assertFalse(result)
        post.assert_not_called()


class DiscordBotGeminiTests(unittest.TestCase):
    def test_classifier_uses_supported_genai_client_and_flash_lite(self):
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
            model="gemini-2.5-flash-lite",
            contents=ANY,
        )
        self.assertEqual(result["action"], "status")


if __name__ == "__main__":
    unittest.main()

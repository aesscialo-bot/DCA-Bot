"""Offline Discord entry-point and response-delivery regressions."""

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
import discord_bot as bot


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Message:
    def __init__(self, content, *, author_id="123", channel_id="456", is_bot=False):
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=is_bot)
        self.channel = SimpleNamespace(id=channel_id, typing=_Typing)
        self.mentions = []
        self.reply = AsyncMock()


def _http_error(error_type, status, detail="test Discord failure"):
    return error_type(
        SimpleNamespace(status=status, reason="test response"),
        {"code": 50013 if status == 403 else 0, "message": detail},
    )


class DiscordMessageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bot, "ALLOWED_USERS", "123"))
        self.stack.enter_context(patch.object(bot, "CHANNEL_ID", "456"))
        self.stack.enter_context(patch.object(bot, "GEMINI_API_KEY", "offline-test-key"))
        self.logs = self.stack.enter_context(patch.object(bot, "_log"))
        self.network = self.stack.enter_context(patch.object(
            bot.requests.sessions.Session, "request",
            side_effect=AssertionError("Production HTTP is forbidden in this test"),
        ))
        self.gemini = self.stack.enter_context(patch.object(
            bot.genai, "Client",
            side_effect=AssertionError("Production Gemini is forbidden in this test"),
        ))
        self.dispatch = self.stack.enter_context(patch.object(
            bot, "trigger_workflow",
            side_effect=AssertionError("Production workflow dispatch is forbidden in this test"),
        ))

    def tearDown(self):
        self.network.assert_not_called()
        self.gemini.assert_not_called()

    def _replace_read_handlers(self):
        handlers = {}
        for action in ("help", "status", "health", "portfolio"):
            async def reply(params, message, name=action):
                await message.reply(f"{name} response")
            handlers[action] = AsyncMock(side_effect=reply)
            self.stack.enter_context(patch.object(bot, f"handle_{action}", handlers[action]))
        self.stack.enter_context(patch.dict(bot.READ_ONLY_ACTION_HANDLERS, handlers, clear=True))
        return handlers

    async def test_direct_read_aliases_reply_without_entering_gemini_classification(self):
        handlers = self._replace_read_handlers()
        classifier = self.stack.enter_context(patch.object(
            bot, "classify_intent", AsyncMock(side_effect=AssertionError("Alias used classifier")),
        ))
        cases = {
            "help": "help", "!help": "help", " HELP?! ": "help", "!dca help": "help",
            "status": "status", " Show\tstatus. ": "status", "!dca status": "status",
            "health": "health", "SHOW HEALTH!": "health", "!dca health": "health",
            "portfolio": "portfolio", "show portfolio": "portfolio", "!dca portfolio": "portfolio",
        }
        for content, action in cases.items():
            with self.subTest(content=content):
                for handler in handlers.values():
                    handler.reset_mock()
                message = _Message(content)
                await bot.on_message(message)
                handlers[action].assert_awaited_once_with({}, message)
                for other_action, handler in handlers.items():
                    if other_action != action:
                        handler.assert_not_awaited()
                message.reply.assert_awaited_once_with(f"{action} response")
        classifier.assert_not_awaited()
        self.dispatch.assert_not_called()

    async def test_real_help_entry_point_delivers_complete_command_guide(self):
        message = _Message("help")
        await bot.on_message(message)
        cards = [call.kwargs["embed"].description for call in message.reply.await_args_list]
        self.assertEqual("".join(cards), bot.HELP_TEXT)
        for command in ("`status`", "`health`", "`portfolio`", "`!dca analyze all`"):
            self.assertIn(command, "".join(cards))
        self.assertTrue(all(len(card.encode("utf-16-le")) // 2 <= 1900 for card in cards))
        self.dispatch.assert_not_called()

    async def test_wrong_user_wrong_channel_and_bot_messages_remain_silent(self):
        processor = self.stack.enter_context(patch.object(bot, "_process_authorized_message", AsyncMock()))
        for identity in (
            {"author_id": "999"}, {"channel_id": "999"}, {"is_bot": True},
        ):
            with self.subTest(identity=identity):
                message = _Message("help", **identity)
                await bot.on_message(message)
                message.reply.assert_not_awaited()
        processor.assert_not_awaited()
        self.logs.assert_not_called()
        self.dispatch.assert_not_called()

    async def test_missing_allowlist_or_channel_does_not_expose_private_reads(self):
        processor = self.stack.enter_context(patch.object(bot, "_process_authorized_message", AsyncMock()))
        for setting in ("ALLOWED_USERS", "CHANNEL_ID"):
            with self.subTest(setting=setting), patch.object(bot, setting, ""):
                message = _Message("status")
                await bot.on_message(message)
                message.reply.assert_not_awaited()
        processor.assert_not_awaited()
        self.dispatch.assert_not_called()

    async def test_common_natural_language_routes_to_the_requested_read_handler(self):
        handlers = self._replace_read_handlers()
        cases = {
            "When is the next buy?": "status",
            "Why are you not buying?": "status",
            "Are you working?": "health",
            "What commands can I use?": "help",
            "Show my holdings": "portfolio",
        }
        for content, action in cases.items():
            with self.subTest(content=content):
                for handler in handlers.values():
                    handler.reset_mock()
                message = _Message(content)
                await bot.on_message(message)
                handlers[action].assert_awaited_once_with({}, message)
                self.assertEqual(sum(handler.await_count for handler in handlers.values()), 1)
                message.reply.assert_awaited_once_with(f"{action} response")
        self.dispatch.assert_not_called()

    async def test_greeting_and_explanation_deliver_reviewed_replies(self):
        for content, topic in (("Hello!", "greeting"), ("What is DCA?", "dca")):
            with self.subTest(content=content):
                message = _Message(content)
                await bot.on_message(message)
                message.reply.assert_awaited_once_with(bot.CHAT_TOPIC_REPLIES[topic])
        self.dispatch.assert_not_called()

    async def test_natural_language_change_request_only_explains_exact_controls(self):
        handlers = self._replace_read_handlers()
        for content in ("Please enable ETH and buy it now", "Show status and buy ETH"):
            with self.subTest(content=content):
                message = _Message(content)
                await bot.on_message(message)
                message.reply.assert_awaited_once_with(bot.CHAT_TOPIC_REPLIES["controls"])
        for handler in handlers.values():
            handler.assert_not_awaited()
        self.dispatch.assert_not_called()

    async def test_definite_embed_rejection_falls_back_to_same_plain_text(self):
        message = _Message("help")
        message.reply.side_effect = [_http_error(discord.Forbidden, 403), None, None]
        await bot._reply_sections(message, ["First complete section", "Second complete section"])
        self.assertEqual(message.reply.await_count, 3)
        first, fallback, second = message.reply.await_args_list
        self.assertEqual(first.kwargs["embed"].description, "First complete section")
        self.assertEqual(fallback.args, ("First complete section",))
        self.assertNotIn("embed", fallback.kwargs)
        self.assertEqual(second.kwargs["embed"].description, "Second complete section")
        for call in message.reply.await_args_list:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.dispatch.assert_not_called()

    async def test_uncertain_http_failure_does_not_retry_or_send_later_sections(self):
        message = _Message("help")
        failure = _http_error(discord.HTTPException, 500)
        message.reply.side_effect = failure
        with self.assertRaises(discord.HTTPException) as raised:
            await bot._reply_sections(message, ["First section", "Second section"])
        self.assertIs(raised.exception, failure)
        message.reply.assert_awaited_once()
        self.dispatch.assert_not_called()

    async def test_handler_error_returns_safe_feedback_without_exposing_exception(self):
        secret = "private-api-key-must-not-appear"
        handlers = self._replace_read_handlers()
        handlers["status"].side_effect = RuntimeError(f"Remote error: {secret}")
        message = _Message("status")
        await bot.on_message(message)
        handlers["status"].assert_awaited_once()
        message.reply.assert_awaited_once()
        reply = message.reply.await_args.args[0]
        self.assertIn("couldn't finish", reply)
        self.assertIn("`help`", reply)
        self.assertIn("`status`", reply)
        self.assertIn("check its workflow before retrying", reply)
        self.assertNotIn(secret, reply)
        log_text = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertIn("RuntimeError", log_text)
        self.assertNotIn(secret, log_text)
        self.dispatch.assert_not_called()

    async def test_reply_failure_after_accepted_analysis_never_retries_workflow(self):
        secret = "sensitive-response-body-must-stay-private"
        self.dispatch.side_effect = None
        self.dispatch.return_value = True
        message = _Message("!dca analyze all")
        message.reply.side_effect = [RuntimeError(secret), None]
        await bot.on_message(message)
        self.dispatch.assert_called_once_with("crypto_analysis.yml", {"symbol": "all"})
        self.assertEqual(message.reply.await_count, 2)
        feedback = message.reply.await_args.args[0]
        self.assertIn("may already have been queued", feedback)
        self.assertNotIn(secret, feedback)
        self.assertNotIn(secret, "\n".join(call.args[0] for call in self.logs.call_args_list))

    async def test_error_feedback_failure_is_bounded_and_logs_only_error_types(self):
        secret = "do-not-log-discord-response-body"
        handlers = self._replace_read_handlers()
        handlers["health"].side_effect = ValueError(secret)
        message = _Message("health")
        message.reply.side_effect = _http_error(discord.Forbidden, 403, detail=secret)
        await bot.on_message(message)
        handlers["health"].assert_awaited_once()
        message.reply.assert_awaited_once()
        log_text = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertIn("ValueError", log_text)
        self.assertIn("Forbidden", log_text)
        self.assertNotIn(secret, log_text)
        self.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

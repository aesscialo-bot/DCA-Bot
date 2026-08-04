import asyncio
import json
import unittest
from unittest.mock import patch

import discord_bot


class MessageStub:
    def __init__(self):
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)


class DiscordBotDcaAmountTests(unittest.TestCase):
    def setUp(self):
        self.target_map = {
            "BTC_THB": {
                "TIME": "00:45",
                "AMOUNT": 600,
                "BUY_ENABLED": True,
                "LAST_BUY_DATE": "2026-07-16",
                "DYNAMIC_DCA": {"ENABLED": True},
            }
        }

    def test_amount_limits_are_inclusive(self):
        for amount in (50, 2000):
            with self.subTest(amount=amount):
                message = MessageStub()
                saved = {}

                with (
                    patch.object(
                        discord_bot,
                        "_get_repo_variable_and_refresh",
                        return_value=json.dumps(self.target_map),
                    ),
                    patch.object(
                        discord_bot,
                        "update_repo_variable",
                        side_effect=lambda name, value: saved.update(
                            {"name": name, "value": value}
                        )
                        or True,
                    ),
                ):
                    asyncio.run(
                        discord_bot.handle_update_dca(
                            {"symbol": "BTC", "field": "AMOUNT", "value": amount},
                            message,
                        )
                    )

                updated_map = json.loads(saved["value"])
                self.assertEqual(saved["name"], "DCA_TARGET_MAP")
                self.assertEqual(updated_map["BTC_THB"]["AMOUNT"], amount)
                self.assertEqual(
                    updated_map["BTC_THB"]["DYNAMIC_DCA"],
                    self.target_map["BTC_THB"]["DYNAMIC_DCA"],
                )

    def test_amounts_outside_limits_are_rejected_without_writing(self):
        for amount in (49, 2000.01):
            with self.subTest(amount=amount):
                message = MessageStub()

                with patch.object(discord_bot, "update_repo_variable") as write:
                    asyncio.run(
                        discord_bot.handle_update_dca(
                            {"symbol": "BTC", "field": "AMOUNT", "value": amount},
                            message,
                        )
                    )

                write.assert_not_called()
                self.assertIn("between 50 and 2000", message.replies[0])


class DiscordBotWorkflowDispatchTests(unittest.TestCase):
    @patch.object(discord_bot.requests, "post")
    def test_dispatch_uses_configured_workflow_ref(self, post):
        post.return_value.status_code = 204

        with patch.object(
            discord_bot, "GITHUB_WORKFLOW_REF", "codex/kraken-gbp"
        ):
            result = discord_bot.trigger_workflow("daily_dca.yml")

        self.assertTrue(result)
        self.assertEqual(
            post.call_args.kwargs["json"], {"ref": "codex/kraken-gbp"}
        )

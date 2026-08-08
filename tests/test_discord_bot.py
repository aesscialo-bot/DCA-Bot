import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import discord_bot
from dca_config import (
    ANALYSIS_STATE_VERSION,
    ALLOWED_TARGETS,
    TIMING_POLICY_VERSION,
    rules_hash,
)


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
        selected_at = execute_at
        analysis_date = generated_at.astimezone(discord_bot.TIMEZONE).date().isoformat()
        targets[symbol] = {
            "ENABLED": bool(live_rules[symbol]["BUY_ENABLED"]),
            "ANALYSIS_STATUS": status,
            "EXECUTION_STATUS": "ARMED" if status == "READY" else "BLOCKED",
            "REGIME": "UPTREND" if status == "READY" else None,
            "AMOUNT_TIER": "LOW" if status == "READY" else None,
            "SELECTED_AT": selected_at.isoformat().replace("+00:00", "Z")
            if status == "READY" else None,
            "EXECUTE_AT": execute_at.isoformat().replace("+00:00", "Z")
            if status == "READY"
            else None,
            "VALID_UNTIL": (execute_at + timedelta(minutes=60))
            .isoformat()
            .replace("+00:00", "Z")
            if status == "READY"
            else None,
            "CATCHUP_APPLIED": False,
            "DECISION_ID": f"decision-{symbol.lower()}",
            "RULES_HASH": rules_hash(symbol, live_rules[symbol]),
            "POLICY_VERSION": TIMING_POLICY_VERSION,
            "ANALYSIS_DATE": analysis_date,
            "HISTORY": (
                {
                    "STATUS": "READY",
                    "FROM": (generated_at - timedelta(days=65))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "THROUGH": (generated_at - timedelta(minutes=15))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "HASH": "a" * 64,
                }
                if status == "READY" else {"STATUS": "ERROR"}
            ),
            "SIGNALS": {},
            "TIMING": {
                "ANALYZED_AT": generated_at.isoformat().replace("+00:00", "Z")
            },
            "ERROR": None if status == "READY" else "test analysis error",
        }
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_at.isoformat().replace("+00:00", "Z"),
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": generated_at.astimezone(discord_bot.TIMEZONE).date().isoformat(),
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
    target = f"{symbol}_GBP"
    event = {
        "event_version": 3, "event_id": delivery_id, "occurred_at": created_at,
        "target": target, "base_currency": symbol, "quote_currency": "GBP",
        "budget_currency": "GBP", "funding_order_id": None,
        "crypto_order_id": delivery_id, "gbp_debit": "10", "gbp_usd_rate": "1.3",
        "funded_usd": "0", "route": "DIRECT_GBP", "crypto_cost_quote": "10",
        "crypto_quantity": "0.0002", "unit_price_quote": "50000",
        "funding_fee_quote": "0", "crypto_fee_quote": "0.08",
    }
    event["canonical_hash"] = sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "version": 3,
        "delivery_id": delivery_id,
        "created_at": created_at,
        "symbol": symbol,
        "row": row,
        "row_sha256": sha256(row.encode("utf-8")).hexdigest(),
        "event": event,
        "event_sha256": sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
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
        discord_bot._awaiting_daily_analysis = False
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        discord_bot._schedule_start_date = None
        discord_bot._analysis_watchdog_last_dispatch = None
        discord_bot._workflow_contract_error = None
        self.workflow_health = {
            "status": "HEALTHY",
            "configured_ref": "main",
            "actual_ref": "main",
            "head_sha": "0123456789abcdef",
            "run_status": "completed",
            "conclusion": "success",
            "updated_at": "2026-08-05T03:55:00Z",
            "run_number": 42,
            "reason": None,
        }
        self.workflow_health_patch = patch.object(
            discord_bot,
            "get_analysis_workflow_health",
            return_value=self.workflow_health,
        )
        self.workflow_health_patch.start()
        self.addCleanup(self.workflow_health_patch.stop)
        self.rules = rules()
        self.analysis = analysis_state(self.rules)

    def test_only_three_production_usd_assets_are_accepted(self):
        self.assertEqual(discord_bot._normalise_usd_key("bitcoin"), "BTC_GBP")
        self.assertEqual(discord_bot._normalise_usd_key("HYPE/USD"), "HYPE_USD")
        self.assertEqual(discord_bot._normalise_usd_key("BTC/GBP"), "BTC_GBP")
        with self.assertRaisesRegex(ValueError, "Supported assets"):
            discord_bot._normalise_usd_key("CAR")

    def test_ghostfolio_health_distinguishes_pending_and_completed_receipts(self):
        event = gist_delivery()["event"]
        files = {
            "portfolio/events.jsonl": SimpleNamespace(
                content=json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
                exists=True,
            ),
            "portfolio/receipts.jsonl": SimpleNamespace(content="", exists=True),
        }
        client = MagicMock()
        client.resolve_commit_sha.return_value = "a" * 40
        client.read_text_at_commit.side_effect = lambda path, _sha: files[path]
        with (
            patch.object(discord_bot, "GH_PAT", "controller-token"),
            patch.object(discord_bot, "DCA_OUTBOX_EVENT_PATH", "portfolio/events.jsonl"),
            patch.object(discord_bot, "DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH", "portfolio/receipts.jsonl"),
            patch.object(discord_bot, "GitHubContentsClient", return_value=client),
        ):
            self.assertEqual(
                discord_bot.get_ghostfolio_delivery_health(),
                {"status": "PENDING", "pending": 1, "completed": 0},
            )
            receipt = {
                "order_id": event["event_id"],
                "event_hash": event["canonical_hash"],
                "ghostfolio_activity_id": "activity-id",
                "imported_at": "2026-08-06T01:10:00Z",
            }
            files["portfolio/receipts.jsonl"].content = json.dumps(receipt) + "\n"
            self.assertEqual(
                discord_bot.get_ghostfolio_delivery_health(),
                {"status": "CLEAR", "pending": 0, "completed": 1},
            )

    def test_ghostfolio_health_rejects_malformed_event_hash(self):
        event = gist_delivery()["event"]
        event["canonical_hash"] = "0" * 64
        files = {
            "portfolio/events.jsonl": SimpleNamespace(content=json.dumps(event) + "\n", exists=True),
            "portfolio/receipts.jsonl": SimpleNamespace(content="", exists=True),
        }
        client = MagicMock()
        client.resolve_commit_sha.return_value = "b" * 40
        client.read_text_at_commit.side_effect = lambda path, _sha: files[path]
        with (
            patch.object(discord_bot, "GH_PAT", "controller-token"),
            patch.object(discord_bot, "DCA_OUTBOX_EVENT_PATH", "portfolio/events.jsonl"),
            patch.object(discord_bot, "DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH", "portfolio/receipts.jsonl"),
            patch.object(discord_bot, "GitHubContentsClient", return_value=client),
        ):
            self.assertEqual(
                discord_bot.get_ghostfolio_delivery_health()["status"], "INVALID"
            )

    def test_ghostfolio_health_uses_exact_private_target_and_safe_token_precedence(self):
        for dedicated, controller, expected in (
            ("dedicated-token", "controller-token", "dedicated-token"),
            ("", "controller-token", "controller-token"),
        ):
            with self.subTest(dedicated=bool(dedicated)):
                client = MagicMock()
                client.resolve_commit_sha.return_value = "c" * 40
                client.read_text_at_commit.side_effect = (
                    lambda _path, _sha: SimpleNamespace(content="", exists=True)
                )
                with (
                    patch.dict(
                        discord_bot.os.environ,
                        {discord_bot.TOKEN_ENV: dedicated},
                        clear=False,
                    ),
                    patch.object(discord_bot, "GH_PAT", controller),
                    patch.object(discord_bot, "GitHubContentsClient", return_value=client) as client_type,
                ):
                    self.assertEqual(
                        discord_bot.get_ghostfolio_delivery_health(),
                        {"status": "CLEAR", "pending": 0, "completed": 0},
                    )
                self.assertEqual(
                    client_type.call_args.kwargs,
                    {
                        "owner": "aesscialo-bot",
                        "repository": "portfolio-canonical-ledger",
                        "branch": "main",
                        "token": expected,
                    },
                )
                self.assertEqual(
                    client.read_text_at_commit.call_args_list[0].args,
                    ("portfolio/kraken_usd_dca_ghostfolio_events.jsonl", "c" * 40),
                )
                self.assertEqual(
                    client.read_text_at_commit.call_args_list[1].args,
                    ("portfolio/ghostfolio_sync_receipts.jsonl", "c" * 40),
                )

    def test_ghostfolio_health_fails_closed_without_any_repository_token(self):
        with (
            patch.dict(discord_bot.os.environ, {discord_bot.TOKEN_ENV: ""}, clear=False),
            patch.object(discord_bot, "GH_PAT", ""),
            patch.object(discord_bot, "GitHubContentsClient") as client_type,
        ):
            self.assertEqual(
                discord_bot.get_ghostfolio_delivery_health(),
                {"status": "UNAVAILABLE", "pending": None, "completed": None},
            )
        client_type.assert_not_called()

    def test_symbols_are_derived_from_valid_three_target_map(self):
        with patch.object(
            discord_bot,
            "_get_repo_variable_and_refresh",
            return_value=json.dumps(self.rules),
        ):
            self.assertEqual(
                discord_bot._symbols_from_dca_map(),
                "BTC/GBP, HYPE/USD, SOL/GBP",
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
        self.assertIn("sideways midpoint £15", message.replies[-1])

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
            "SOL_GBP": {
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
        self.assertIn("reconciliation is pending for SOL_GBP", message.replies[-1])

    def test_enable_review_allows_pending_portfolio_ledger_delivery(self):
        execution = {
            "SOL_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery("ORDER-SOL-1", symbol="SOL")
                ],
            }
        }

        review = discord_bot._enable_review(
            "BTC_GBP",
            self.rules,
            self.analysis,
            execution,
            now=NOW,
        )

        self.assertEqual(review["symbol"], "BTC_GBP")

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
        analysis["TARGETS"]["SOL_GBP"].update(
            {
                "ANALYSIS_STATUS": "ERROR",
                "EXECUTION_STATUS": "BLOCKED",
                "REGIME": None,
                "AMOUNT_TIER": None,
                "SELECTED_AT": None,
                "EXECUTE_AT": None,
                "VALID_UNTIL": None,
                "HISTORY": {"STATUS": "ERROR"},
                "SIGNALS": {"ERROR": "test"},
                "ERROR": "test",
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
        changed_rules["SOL_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 11
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
        self.assertIn("global three-asset DCA rules changed", message.replies[-1])

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
        self.assertIn("Kraken mixed-market DCA status", status_message.replies[-1])
        self.assertIn("BTC_GBP", status_message.replies[-1])
        self.assertIn("UPTREND/lower £10", status_message.replies[-1])
        self.assertIn("SIDEWAYS/midpoint £15", status_message.replies[-1])
        self.assertIn("DOWNTREND/higher £20", status_message.replies[-1])
        self.assertEqual(status_message.replies[-1].count("Data through:"), 3)
        self.assertIn("2026-08-05 10:45 +07", status_message.replies[-1])
        self.assertIn("GitHub analysis workflow: **HEALTHY**", status_message.replies[-1])
        self.assertIn("actual ref: `main@0123456789ab`", status_message.replies[-1])
        self.assertIn("Analysis watchdog: **SATISFIED**", status_message.replies[-1])
        self.assertIn("Analysis/scheduling chain: **OPERATIONAL**", status_message.replies[-1])
        self.assertIn("ready-but-disabled", status_message.replies[-1])
        self.assertLessEqual(len(status_message.replies[-1]), 2_000)

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
        self.assertIn("Kraken mixed-market DCA controls", discord_bot.HELP_TEXT)
        self.assertLessEqual(len(health_message.replies[-1]), 2_000)

    def test_status_and_health_label_unknown_workflow_evidence_fail_closed(self):
        unknown = {
            **self.workflow_health,
            "status": "UNKNOWN",
            "actual_ref": None,
            "head_sha": None,
            "run_status": None,
            "conclusion": None,
            "updated_at": None,
            "reason": "GitHub workflow evidence is unavailable",
        }
        status_message = MessageStub()
        health_message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(self.rules, self.analysis),
            ),
            patch.object(discord_bot, "get_analysis_workflow_health", return_value=unknown),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            asyncio.run(discord_bot.handle_status({}, status_message))
            asyncio.run(discord_bot.handle_health({}, health_message))

        status = status_message.replies[-1]
        health = health_message.replies[-1]
        self.assertIn("GitHub analysis workflow: **UNKNOWN**", status)
        self.assertIn("Railway scheduler: **running", status)
        self.assertIn("Analysis/scheduling chain: **UNKNOWN**", status)
        self.assertNotIn("ready-but-disabled", status)
        self.assertIn("DCA health: ATTENTION REQUIRED", health)
        self.assertIn("Analysis/scheduling chain: UNKNOWN", health)

    def test_missing_history_through_is_reported_as_unknown(self):
        decision = deepcopy(self.analysis["TARGETS"]["BTC_GBP"])
        decision["HISTORY"].pop("THROUGH")

        summary = discord_bot._decision_summary(
            "BTC_GBP",
            self.rules["BTC_GBP"],
            decision,
            {},
            now=NOW,
        )

        self.assertIn("Data through: `unknown`", summary)

    def test_status_distinguishes_enabled_shadow_disabled_and_budget_refresh(self):
        live_rules = rules(enabled={"BTC_GBP", "SOL_GBP"}, low=10, up=20)
        analysis = analysis_state(live_rules)
        live_rules["SOL_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 12.5
        message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(live_rules, analysis),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
            patch.object(discord_bot, "DCA_TRADING_MODE", "shadow"),
        ):
            asyncio.run(discord_bot.handle_status({}, message))

        status = message.replies[-1]
        self.assertIn("SHADOW — REAL KRAKEN ORDERS OFF", status)
        self.assertIn("BTC/GBP", status)
        self.assertIn("SIMULATION ONLY", status)
        self.assertIn("HYPE/USD", status)
        self.assertIn("OFF — PAIR DISABLED", status)
        self.assertIn("SOL/GBP", status)
        self.assertIn("WAITING FOR FRESH ANALYSIS", status)
        self.assertIn("budgets changed after this analysis", status)
        self.assertNotIn("RULES MISMATCH", status)
        self.assertLessEqual(len(status), 2_000)

    def test_expired_enabled_pair_says_no_replay_and_resumes_tomorrow(self):
        live_rules = rules(enabled={"HYPE_USD"})
        decision = analysis_state(live_rules)["TARGETS"]["HYPE_USD"]

        summary = discord_bot._decision_summary(
            "HYPE_USD",
            live_rules["HYPE_USD"],
            decision,
            {},
            now=NOW + timedelta(hours=3),
        )

        self.assertIn("DONE FOR TODAY — NEXT ANALYSIS TOMORROW", summary)
        self.assertIn("no late order will be replayed", summary)
        self.assertIn("tomorrow's analysis resumes it", summary)
        self.assertIn(f"Regime: `{decision['REGIME']}`", summary)
        self.assertNotIn("Regime: `ERROR`", summary)

    def test_expired_enabled_pair_is_quietly_omitted_from_scheduler(self):
        live_rules = rules(enabled={"HYPE_USD"})
        analysis = analysis_state(live_rules)
        execution = {}

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(analysis),
                json.dumps(execution),
                "2026-08-05",
                now=NOW + timedelta(hours=3),
            )
        )

        self.assertNotIn("HYPE_USD", discord_bot._dca_schedule)
        self.assertIsNone(discord_bot._schedule_error)
        self.assertIsNone(discord_bot._schedule_warning)

    def test_chain_never_reports_operational_for_unhealthy_workflow_evidence(self):
        with patch.object(discord_bot, "DCA_CRON_ENABLED", True):
            for workflow_status in ("UNKNOWN", "FAILING", "BLOCKED", "STALE"):
                with self.subTest(workflow_status=workflow_status):
                    self.assertEqual(
                        discord_bot._analysis_chain_status(
                            local_scheduler_ok=True,
                            workflow_health={"status": workflow_status},
                            watchdog_health={"status": "SATISFIED"},
                        ),
                        "UNKNOWN"
                        if workflow_status == "UNKNOWN"
                        else "ATTENTION REQUIRED",
                    )

    def test_status_and_health_separate_portfolio_delivery_from_kraken_recovery(self):
        execution = {
            "BTC_GBP": {
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
        self.assertIn("awaiting 04:07 start-day analysis", message.replies[-1])
        self.assertNotIn("ATTENTION REQUIRED", message.replies[-1])
        self.assertNotIn("Analysis ERROR", message.replies[-1])

    def test_status_and_health_report_armed_during_daily_rollover_wait(self):
        live_rules = rules(enabled=set(ALLOWED_TARGETS))
        decisions = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
        )
        rollover = datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc)
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-01",
                now=rollover,
            )
        )
        status_message = MessageStub()
        health_message = MessageStub()
        with (
            patch.object(
                discord_bot,
                "get_repo_variable",
                side_effect=variable_reader(live_rules, decisions),
            ),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            FrozenDateTime.current = rollover
            try:
                asyncio.run(discord_bot.handle_status({}, status_message))
                asyncio.run(discord_bot.handle_health({}, health_message))
            finally:
                FrozenDateTime.current = NOW

        status = status_message.replies[-1]
        health = health_message.replies[-1]
        self.assertIn("awaiting 04:07 daily analysis", status)
        self.assertIn("TODAY'S ANALYSIS NOT DUE YET", status)
        self.assertNotIn("NEXT ANALYSIS TOMORROW", status)
        self.assertIn("Analysis watchdog: **WAITING**", status)
        self.assertIn("Analysis/scheduling chain: **OPERATIONAL**", status)
        self.assertIn("DCA health: ARMED", health)
        self.assertIn("Analysis: awaiting 04:07 daily analysis", health)
        self.assertNotIn("Stale decisions", health)

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
        discord_bot._awaiting_daily_analysis = False
        discord_bot._dca_dispatch_guard.clear()
        discord_bot._schedule_error = None
        discord_bot._schedule_warning = None
        discord_bot._schedule_start_date = None
        discord_bot._analysis_watchdog_last_dispatch = None
        discord_bot._workflow_contract_error = None

    def test_v1_analysis_state_clears_schedule_and_fails_closed(self):
        live_rules = rules(enabled={"BTC_GBP", "HYPE_USD", "SOL_GBP"})
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
        self.assertIn("VERSION must be 3", discord_bot._schedule_error)

    def test_multiple_assets_can_share_or_use_different_absolute_times(self):
        live_rules = rules(enabled={"BTC_GBP", "HYPE_USD", "SOL_GBP"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={
                "BTC_GBP": 30,
                "HYPE_USD": 30,
                "SOL_GBP": 45,
            },
        )
        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(
            set(discord_bot._dca_schedule),
            {"BTC_GBP", "HYPE_USD", "SOL_GBP"},
        )
        self.assertEqual(
            discord_bot._dca_schedule["BTC_GBP"]["execute_at"],
            discord_bot._dca_schedule["HYPE_USD"]["execute_at"],
        )
        self.assertNotEqual(
            discord_bot._dca_schedule["BTC_GBP"]["execute_at"],
            discord_bot._dca_schedule["SOL_GBP"]["execute_at"],
        )

    def test_scheduler_arms_without_warning_before_start_day_analysis(self):
        live_rules = rules(enabled={"BTC_GBP"})
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
        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_GBP": "ERROR"}
        )
        before_deadline = datetime(2026, 8, 5, 21, 19, tzinfo=timezone.utc)
        after_deadline = datetime(2026, 8, 5, 21, 20, tzinfo=timezone.utc)

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
        self.assertIn("BTC_GBP: analysis ERROR", discord_bot._schedule_warning)

    def test_prior_day_ready_state_waits_quietly_until_daily_analysis_deadline(self):
        live_rules = rules(enabled={"BTC_GBP", "HYPE_USD", "SOL_GBP"})
        decisions = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
        )

        for current in (
            datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 5, 21, 19, 59, tzinfo=timezone.utc),
        ):
            with self.subTest(current=current):
                self.assertTrue(
                    discord_bot.refresh_dca_schedule(
                        json.dumps(live_rules),
                        json.dumps(decisions),
                        "{}",
                        "2026-08-01",
                        now=current,
                    )
                )
                self.assertEqual(discord_bot._dca_schedule, {})
                self.assertIsNone(discord_bot._schedule_warning)
                self.assertTrue(discord_bot._awaiting_daily_analysis)
                with patch.object(discord_bot, "DCA_CRON_ENABLED", True):
                    self.assertIn(
                        "awaiting 04:07 daily analysis",
                        discord_bot._format_cron_status(),
                    )

    def test_prior_day_ready_state_alerts_at_daily_analysis_deadline(self):
        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
        )
        deadline = datetime(2026, 8, 5, 21, 20, tzinfo=timezone.utc)

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules),
                json.dumps(decisions),
                "{}",
                "2026-08-01",
                now=deadline,
            )
        )
        self.assertFalse(discord_bot._awaiting_daily_analysis)
        self.assertIn("stale analysis date", discord_bot._schedule_warning)

    def test_unhealthy_or_older_prior_state_is_not_quiet_during_rollover(self):
        live_rules = rules(enabled={"BTC_GBP"})
        rollover = datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc)
        unhealthy = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
            status_overrides={"BTC_GBP": "ERROR"},
        )
        older = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 4, 12, 20, tzinfo=timezone.utc),
        )

        for decisions in (unhealthy, older):
            with self.subTest(state_date=decisions["ANALYSIS_DATE"]):
                self.assertTrue(
                    discord_bot.refresh_dca_schedule(
                        json.dumps(live_rules),
                        json.dumps(decisions),
                        "{}",
                        "2026-08-01",
                        now=rollover,
                    )
                )
                self.assertFalse(discord_bot._awaiting_daily_analysis)
                self.assertIsNotNone(discord_bot._schedule_warning)

    def test_analysis_watchdog_waits_until_boundary_then_dispatches(self):
        live_rules = rules(enabled=set(ALLOWED_TARGETS))
        decisions = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
        )
        before_deadline = datetime(2026, 8, 5, 21, 19, 59, tzinfo=timezone.utc)
        deadline = datetime(2026, 8, 5, 21, 20, tzinfo=timezone.utc)

        with (
            patch.object(discord_bot, "analysis_workflow_active") as active,
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot._analysis_watchdog(
                    json.dumps(decisions), before_deadline
                )
            )
        active.assert_not_called()
        dispatch.assert_not_called()

        with (
            patch.object(
                discord_bot, "analysis_workflow_active", return_value=False
            ),
            patch.object(discord_bot, "trigger_workflow", return_value=True) as dispatch,
            patch.object(discord_bot, "monotonic", return_value=100.0),
        ):
            asyncio.run(
                discord_bot._analysis_watchdog(json.dumps(decisions), deadline)
            )
        dispatch.assert_called_once_with("crypto_analysis.yml", {"symbol": "all"})
        self.assertEqual(discord_bot._analysis_watchdog_last_dispatch, 100.0)

    def test_analysis_watchdog_suppresses_active_and_recent_recovery(self):
        live_rules = rules(enabled=set(ALLOWED_TARGETS))
        decisions = analysis_state(
            live_rules,
            generated_at=datetime(2026, 8, 5, 12, 20, tzinfo=timezone.utc),
        )
        deadline = datetime(2026, 8, 5, 21, 20, tzinfo=timezone.utc)

        with (
            patch.object(discord_bot, "analysis_workflow_active", return_value=True),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(
                discord_bot._analysis_watchdog(json.dumps(decisions), deadline)
            )
        dispatch.assert_not_called()

        discord_bot._analysis_watchdog_last_dispatch = 100.0
        with (
            patch.object(
                discord_bot, "analysis_workflow_active", return_value=False
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
            patch.object(discord_bot, "monotonic", return_value=101.0),
        ):
            asyncio.run(
                discord_bot._analysis_watchdog(json.dumps(decisions), deadline)
            )
        dispatch.assert_not_called()

    def test_fresh_start_day_analysis_schedules_during_grace(self):
        live_rules = rules(enabled={"BTC_GBP"})
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
        self.assertEqual(set(discord_bot._dca_schedule), {"BTC_GBP"})
        self.assertIsNone(discord_bot._schedule_warning)

        with patch.object(discord_bot, "datetime", FrozenDateTime):
            FrozenDateTime.current = analysis_time
            try:
                self.assertNotIn(
                    "awaiting 04:07", discord_bot._format_cron_status()
                )
            finally:
                FrozenDateTime.current = NOW

    def test_fresh_start_day_analysis_error_alerts_during_grace(self):
        live_rules = rules(enabled={"BTC_GBP"})
        analysis_time = datetime(2026, 8, 5, 21, 1, tzinfo=timezone.utc)
        decisions = analysis_state(
            live_rules,
            generated_at=analysis_time,
            status_overrides={"BTC_GBP": "ERROR"},
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
        self.assertIn("BTC_GBP: analysis ERROR", discord_bot._schedule_warning)
        self.assertEqual(discord_bot._awaiting_start_day_symbols, set())

    def test_disabled_asset_rules_mismatch_does_not_block_enabled_asset(self):
        live_rules = rules(enabled={"HYPE_USD"})
        decisions = analysis_state(live_rules)
        live_rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 11, "UP": 21}

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(set(discord_bot._dca_schedule), {"HYPE_USD"})

    def test_enabled_pair_error_blocks_all_new_order_schedules(self):
        live_rules = rules(enabled={"BTC_GBP", "HYPE_USD"})
        decisions = analysis_state(
            live_rules, status_overrides={"BTC_GBP": "ERROR"}
        )

        self.assertTrue(
            discord_bot.refresh_dca_schedule(
                json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
            )
        )
        self.assertEqual(discord_bot._dca_schedule, {})
        self.assertIn("global all-three Kraken history gate", discord_bot._schedule_warning)
        self.assertIn("BTC_GBP: analysis ERROR", discord_bot._schedule_warning)

    def test_due_assets_use_inclusive_minus_five_plus_sixty_window(self):
        live_rules = rules(enabled={"BTC_GBP", "HYPE_USD"})
        decisions = analysis_state(
            live_rules,
            execute_offsets={symbol: 30 for symbol in ALLOWED_TARGETS},
        )
        discord_bot.refresh_dca_schedule(
            json.dumps(live_rules), json.dumps(decisions), "{}", now=NOW
        )
        self.assertEqual(
            discord_bot._due_symbols_for_dispatch(NOW + timedelta(minutes=25)),
            ["BTC_GBP", "HYPE_USD"],
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
        self.assertIn("stale analysis date", discord_bot._schedule_warning)

    def test_start_date_blocks_new_dispatches_until_local_date(self):
        live_rules = rules(enabled={"BTC_GBP"})
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
        live_rules = rules(enabled={"BTC_GBP"})
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
            "SOL_GBP": {
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
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), ["SOL_GBP"])

    def test_pending_gist_delivery_dispatches_without_kraken_recovery(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "SOL_GBP": {
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
        self.assertEqual(discord_bot._pending_gist_delivery_symbols, {"SOL_GBP"})
        self.assertEqual(discord_bot._due_symbols_for_dispatch(NOW), ["SOL_GBP"])

    def test_pending_gist_delivery_does_not_block_new_order_schedule(self):
        live_rules = rules(enabled={"BTC_GBP"})
        decisions = analysis_state(live_rules)
        execution = {
            "SOL_GBP": {
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

        self.assertEqual(set(discord_bot._dca_schedule), {"BTC_GBP"})
        self.assertIsNone(discord_bot._schedule_warning)

    def test_pending_gist_delivery_survives_invalid_analysis_and_dispatches(self):
        live_rules = rules()
        execution = {
            "BTC_GBP": {
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
        self.assertEqual(discord_bot._pending_gist_delivery_symbols, {"BTC_GBP"})
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
            "daily_dca.yml", {"symbols_json": '["BTC_GBP"]'}
        )

    def test_pending_gist_delivery_uses_thirty_minute_dispatch_guard(self):
        live_rules = rules()
        decisions = analysis_state(live_rules)
        execution = {
            "BTC_GBP": {
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
            "daily_dca.yml", {"symbols_json": '["BTC_GBP"]'}
        )

    def test_pending_recovery_survives_invalid_analysis_state(self):
        live_rules = rules()
        execution = {
            "BTC_GBP": {
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
    @patch.object(discord_bot.requests, "get")
    def test_analysis_workflow_health_reports_actual_successful_ref(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "workflow_runs": [
                {
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": "abcdef0123456789",
                    "updated_at": "2026-08-05T03:55:00Z",
                    "run_number": 81,
                }
            ]
        }
        with (
            patch.object(discord_bot, "GH_PAT", "configured"),
            patch.object(discord_bot, "GITHUB_REPO", "owner/repository"),
            patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"),
            patch.object(discord_bot, "_workflow_contract_error", None),
        ):
            health = discord_bot.get_analysis_workflow_health(now=NOW)

        self.assertEqual(health["status"], "HEALTHY")
        self.assertEqual(health["configured_ref"], "main")
        self.assertEqual(health["actual_ref"], "main")
        self.assertEqual(health["head_sha"], "abcdef0123456789")
        self.assertEqual(
            get.call_args.kwargs["params"], {"branch": "main", "per_page": 20}
        )

    @patch.object(discord_bot.requests, "get")
    def test_analysis_workflow_health_blocks_on_auth_failure(self, get):
        get.return_value.status_code = 401
        with (
            patch.object(discord_bot, "GH_PAT", "configured"),
            patch.object(discord_bot, "GITHUB_REPO", "owner/repository"),
            patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"),
            patch.object(discord_bot, "_workflow_contract_error", None),
        ):
            health = discord_bot.get_analysis_workflow_health(now=NOW)

        self.assertEqual(health["status"], "BLOCKED")
        self.assertEqual(health["reason"], "GitHub workflow API HTTP 401")

    @patch.object(discord_bot.requests, "get")
    def test_analysis_workflow_health_preserves_any_active_run_guard(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "workflow_runs": [
                {"status": "completed", "conclusion": "success"},
                {
                    "status": "in_progress",
                    "conclusion": None,
                    "head_branch": "main",
                    "head_sha": "1234567890abcdef",
                },
            ]
        }
        with (
            patch.object(discord_bot, "GH_PAT", "configured"),
            patch.object(discord_bot, "GITHUB_REPO", "owner/repository"),
            patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"),
            patch.object(discord_bot, "_workflow_contract_error", None),
        ):
            health = discord_bot.get_analysis_workflow_health(now=NOW)

        self.assertEqual(health["status"], "ACTIVE")
        self.assertEqual(health["run_status"], "in_progress")

    @patch.object(discord_bot.requests, "get")
    def test_old_success_is_stale_and_cannot_make_chain_operational(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "workflow_runs": [
                {
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": "abcdef0123456789",
                    "updated_at": (NOW - timedelta(hours=31))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "run_number": 80,
                }
            ]
        }
        with (
            patch.object(discord_bot, "GH_PAT", "configured"),
            patch.object(discord_bot, "GITHUB_REPO", "owner/repository"),
            patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"),
            patch.object(discord_bot, "_workflow_contract_error", None),
            patch.object(discord_bot, "DCA_CRON_ENABLED", True),
        ):
            health = discord_bot.get_analysis_workflow_health(now=NOW)
            chain = discord_bot._analysis_chain_status(
                local_scheduler_ok=True,
                workflow_health=health,
                watchdog_health={"status": "ATTENTION"},
            )

        self.assertEqual(health["status"], "STALE")
        self.assertIn("older than 30 hours", health["reason"])
        self.assertEqual(chain, "ATTENTION REQUIRED")

    @patch.object(discord_bot.requests, "get")
    def test_analysis_workflow_health_is_unknown_without_run_evidence(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"workflow_runs": []}
        with (
            patch.object(discord_bot, "GH_PAT", "configured"),
            patch.object(discord_bot, "GITHUB_REPO", "owner/repository"),
            patch.object(discord_bot, "GITHUB_WORKFLOW_REF", "main"),
            patch.object(discord_bot, "_workflow_contract_error", None),
        ):
            health = discord_bot.get_analysis_workflow_health(now=NOW)

        self.assertEqual(health["status"], "UNKNOWN")
        self.assertIn("no workflow runs", health["reason"])

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
        ctor.assert_called_once()
        self.assertEqual(ctor.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(
            ctor.call_args.kwargs["http_options"].timeout,
            discord_bot.GEMINI_TIMEOUT_SECONDS * 1_000,
        )
        client.models.generate_content.assert_called_once_with(
            model="gemini-3.5-flash-lite", contents=ANY, config=ANY
        )
        self.assertEqual(result["action"], "status")

    def test_gemini_model_prose_and_parameters_are_never_executed_or_posted(self):
        intent = discord_bot._validate_intent(
            {
                "action": "chat",
                "topic": "controls",
                "params": {"symbol": "HYPE_USD", "enabled": True},
                "reply": "HYPE enabled and order submitted @everyone",
            }
        )

        self.assertEqual(intent["action"], "chat")
        self.assertEqual(intent["params"], {})
        self.assertEqual(intent["reply"], discord_bot.CHAT_TOPIC_REPLIES["controls"])
        self.assertNotIn("order submitted", intent["reply"])
        self.assertNotIn("@everyone", intent["reply"])
        self.assertNotIn("analyze", discord_bot.READ_ONLY_ACTION_HANDLERS)

    def test_missing_gemini_key_keeps_read_only_natural_language_useful(self):
        with patch.object(discord_bot, "GEMINI_API_KEY", ""):
            status = asyncio.run(
                discord_bot.classify_intent("Why is HYPE disabled today?")
            )
            write = asyncio.run(
                discord_bot.classify_intent("Please enable HYPE and buy it now")
            )

        self.assertEqual(status["action"], "status")
        self.assertEqual(write["action"], "chat")
        self.assertEqual(write["topic"], "controls")

    def test_natural_language_write_request_can_only_receive_read_only_reply(self):
        class TypingStub:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        message = MagicMock()
        message.author = SimpleNamespace(id="123")
        message.channel.id = "456"
        message.channel.typing.return_value = TypingStub()
        message.content = "Please enable HYPE and place its order"
        message.mentions = []
        message.reply = AsyncMock()
        intent = discord_bot._validate_intent(
            {"action": "chat", "topic": "controls"}
        )

        with (
            patch.object(discord_bot, "CHANNEL_ID", "456"),
            patch.object(discord_bot, "ALLOWED_USERS", "123"),
            patch.object(
                discord_bot,
                "classify_intent",
                AsyncMock(return_value=intent),
            ),
            patch.object(discord_bot, "trigger_workflow") as dispatch,
        ):
            asyncio.run(discord_bot.on_message(message))

        message.reply.assert_awaited_once_with(
            discord_bot.CHAT_TOPIC_REPLIES["controls"]
        )
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import copy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

import dca_config
from tests.history_fixtures import ready_history


NOW = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)


def gist_delivery(
    delivery_id="OUF4EM-FRGI2-MQMWZD",
    *,
    symbol="BTC",
    created_at="2026-08-06T01:05:00Z",
    row=None,
):
    if row is None:
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
        "crypto_order_id": delivery_id, "gbp_debit": "10",
        "gbp_usd_rate": "0", "funded_usd": "0", "route": "DIRECT_GBP",
        "crypto_cost_quote": "10", "crypto_quantity": "0.0002",
        "unit_price_quote": "50000", "funding_fee_quote": "0",
        "crypto_fee_quote": "0.08",
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


def sized_gist_row(delivery_id, size):
    suffix = (
        " | two | three | four | five | six | seven | eight | nine | "
        f"{delivery_id} | eleven |\n"
    )
    prefix = "| "
    padding_bytes = size - len((prefix + suffix).encode("utf-8"))
    if padding_bytes < 0:
        raise ValueError("requested row size is too small")
    row = prefix + ("x" * padding_bytes) + suffix
    assert len(row.encode("utf-8")) == size
    return row


def ready_signals():
    return {
        "DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z",
        "DAILY_CLOSE": 105.0,
        "DAILY_PREVIOUS_CLOSE": 104.0,
        "DAILY_TWO_DAYS_AGO_CLOSE": 97.0,
        "DAILY_SMA150": 100.0,
        "DAILY_PREVIOUS_SMA150": 99.0,
        "DAILY_TWO_DAYS_AGO_SMA150": 98.0,
        "DAILY_EMA20": 90.0,
        "DAILY_EMA50": 95.0,
        "DAILY_PREVIOUS_EMA20": 89.0,
        "DAILY_PREVIOUS_EMA50": 94.0,
        "WEEKLY_LAST_COMPLETE": "2026-07-27T00:00:00Z",
        "WEEKLY_CLOSE": 105.0,
        "WEEKLY_EMA20": 100.0,
        "SMA150_SLOPE_20D": -1.0,
        "TWO_DAY_ABOVE": False,
        "TWO_DAY_BELOW": False,
        "THREE_DAY_BELOW": False,
        "WEEKLY_ABOVE": True,
        "WEEKLY_BELOW": False,
        "SLOPE_POSITIVE": False,
        "SLOPE_NEGATIVE": True,
        "UPTREND_CONFIRMATION_REQUIRED": 3,
        "UPTREND_CONFIRMATION_COUNT": 2,
        "UPTREND_CONFIRMED": False,
        "REGIME_WITHOUT_OVERRIDE": "SIDEWAYS",
        "UPTREND_OVERRIDE_ACTIVE": False,
        "UPTREND_OVERRIDE_APPLIED": False,
        "UPTREND_OVERRIDE_REASON": None,
        "UPTREND_OVERRIDE_ACTIVATED_AT": None,
        "UPTREND_OVERRIDE_RELEASED_AT": None,
        "UPTREND_OVERRIDE_AUTO_RELEASED": False,
    }


def ready_state(rules=None):
    rules = rules or dca_config.default_rules_map()
    state = dca_config.empty_analysis_state(rules, now=NOW)
    for target in dca_config.ALLOWED_TARGETS:
        state["TARGETS"][target] = {
            "ENABLED": bool(rules[target]["BUY_ENABLED"]),
            "ANALYSIS_STATUS": "READY",
            "EXECUTION_STATUS": "ARMED",
            "REGIME": "SIDEWAYS",
            "AMOUNT_TIER": "MID",
            "SELECTED_AT": "2026-08-06T01:00:00Z",
            "EXECUTE_AT": "2026-08-06T01:00:00Z",
            "VALID_UNTIL": "2026-08-06T02:00:00Z",
            "CATCHUP_APPLIED": False,
            "DECISION_ID": f"decision-{target.lower()}",
            "RULES_HASH": dca_config.rules_hash(target, rules[target]),
            "POLICY_VERSION": dca_config.TIMING_POLICY_VERSION,
            "ANALYSIS_DATE": "2026-08-06",
            "HISTORY": ready_history(target, "2026-08-05T21:00:00Z"),
            "SIGNALS": ready_signals(),
            "TIMING": {
                "ANALYZED_AT": "2026-08-05T21:00:00Z",
                "SELECTED_LOCAL_TIME": "08:00",
            },
            "ERROR": None,
        }
    state["ANALYSIS_DATE"] = "2026-08-06"
    return state


class RulesSchemaTests(unittest.TestCase):
    def test_global_rules_hash_covers_budgets_and_enable_flags(self):
        rules = dca_config.default_rules_map()
        baseline = dca_config.global_rules_hash(rules)
        changed = copy.deepcopy(rules)
        changed["ETH_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 15}
        self.assertNotEqual(baseline, dca_config.global_rules_hash(changed))
        changed["ETH_GBP"]["BUY_ENABLED"] = True
        self.assertNotEqual(
            dca_config.global_rules_hash(changed),
            dca_config.global_rules_hash({**changed, "ETH_GBP": {
                **changed["ETH_GBP"], "BUY_ENABLED": False
            }}),
        )

    def test_safe_default_has_exact_four_gbp_targets(self):
        rules = dca_config.validate_rules_map(dca_config.default_rules_map())
        self.assertEqual(
            dca_config.ALLOWED_TARGETS,
            ("BTC_GBP", "ETH_GBP", "SOL_GBP", "DOGE_GBP"),
        )
        self.assertEqual(
            dca_config.TARGET_SYMBOLS,
            {
                "BTC_GBP": "BTC/GBP",
                "ETH_GBP": "ETH/GBP",
                "SOL_GBP": "SOL/GBP",
                "DOGE_GBP": "DOGE/GBP",
            },
        )
        self.assertEqual(tuple(rules), dca_config.ALLOWED_TARGETS)
        for rule in rules.values():
            self.assertEqual(
                rule,
                {
                    "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
                    "BUY_ENABLED": False,
                },
            )

    def test_rejects_legacy_quote_targets_unknown_missing_and_legacy_fields(self):
        for legacy_target in ("BTC_USD", "BTC_THB", "HYPE_USD", "ADA_GBP"):
            with self.subTest(legacy_target=legacy_target):
                rules = dca_config.default_rules_map()
                rules[legacy_target] = rules.pop("BTC_GBP")
                with self.assertRaisesRegex(
                    ValueError, f"unsupported targets.*{legacy_target}"
                ):
                    dca_config.validate_rules_map(rules)

        rules = dca_config.default_rules_map()
        rules.pop("ETH_GBP")
        with self.assertRaisesRegex(ValueError, "missing production targets.*ETH_GBP"):
            dca_config.validate_rules_map(rules)

        for legacy in ("TIME", "AMOUNT", "AMOUNT_GBP", "DYNAMIC_DCA"):
            with self.subTest(legacy=legacy):
                rules = dca_config.default_rules_map()
                rules["BTC_GBP"][legacy] = 10
                with self.assertRaisesRegex(ValueError, "unsupported fields"):
                    dca_config.validate_rules_map(rules)

    def test_zero_budget_is_allowed_only_while_disabled(self):
        rules = dca_config.default_rules_map()
        dca_config.validate_rules_map(rules)
        for amount in (0.01, 1, 4.99):
            with self.subTest(amount=amount):
                invalid = dca_config.default_rules_map()
                invalid["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {
                    "LOW": amount,
                    "UP": amount,
                }
                with self.assertRaisesRegex(ValueError, "£0.*or at least £5"):
                    dca_config.validate_rules_map(invalid)
        rules["BTC_GBP"]["BUY_ENABLED"] = True
        with self.assertRaisesRegex(ValueError, "at least £5 before enabling"):
            dca_config.validate_rules_map(rules)

    def test_enabled_budgets_require_bounds_and_live_market_minimum(self):
        rules = dca_config.default_rules_map()
        rules["BTC_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        dca_config.validate_rules_map(rules)
        with self.assertRaisesRegex(ValueError, "below Kraken's current"):
            dca_config.validate_enabled_market_minimums(rules, {"BTC_GBP": 11})
        validated = dca_config.validate_enabled_market_minimums(
            rules, {"BTC/GBP": 10}
        )
        self.assertEqual(validated["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["UP"], 20)

        rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["UP"] = 1000.01
        with self.assertRaisesRegex(ValueError, "between £0 and £1,000"):
            dca_config.validate_rules_map(rules)

    def test_budget_endpoints_are_ordered_and_currency_precision_is_bounded(self):
        rules = dca_config.default_rules_map()
        rules["BTC_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 20, "UP": 10},
            "BUY_ENABLED": False,
        }
        with self.assertRaisesRegex(ValueError, "LOW <= MID <= UP"):
            dca_config.validate_rules_map(rules)

        rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10.001, "UP": 20}
        with self.assertRaisesRegex(ValueError, "no more than two decimal places"):
            dca_config.validate_rules_map(rules)

        rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 10}
        validated = dca_config.validate_rules_map(rules)
        self.assertEqual(
            dca_config.effective_amount_gbp(validated["BTC_GBP"], "SIDEWAYS"),
            10,
        )

    def test_rule_hash_is_stable_and_excludes_buy_enabled(self):
        disabled = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20.0},
            "BUY_ENABLED": False,
        }
        enabled = copy.deepcopy(disabled)
        enabled["BUY_ENABLED"] = True
        baseline_hash = dca_config.rules_hash("BTC_GBP", disabled)
        self.assertEqual(
            baseline_hash,
            dca_config.rules_hash("BTC_GBP", enabled),
        )
        changed = copy.deepcopy(disabled)
        changed["REGIME_AMOUNTS_GBP"]["UP"] = 21
        self.assertNotEqual(
            dca_config.rules_hash("BTC_GBP", disabled),
            dca_config.rules_hash("BTC_GBP", changed),
        )
        with patch.object(
            dca_config,
            "AMOUNT_POLICY_VERSION",
            dca_config.AMOUNT_POLICY_VERSION + 1,
        ):
            self.assertNotEqual(
                baseline_hash,
                dca_config.rules_hash("BTC_GBP", disabled),
            )

    def test_effective_amount_and_aggregate_exposure(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": False,
        }
        self.assertEqual(dca_config.effective_amount(rule, "UPTREND"), 10.0)
        self.assertEqual(dca_config.effective_amount(rule, "SIDEWAYS"), 15.0)
        self.assertEqual(dca_config.effective_amount(rule, "DOWNTREND"), 20.0)
        self.assertEqual(dca_config.amount_tier_for_regime("UPTREND"), "LOW")
        self.assertEqual(dca_config.amount_tier_for_regime("SIDEWAYS"), "MID")
        self.assertEqual(dca_config.amount_tier_for_regime("DOWNTREND"), "HIGH")
        rules = dca_config.default_rules_map()
        rules["BTC_GBP"] = {**rule, "BUY_ENABLED": True}
        rules["ETH_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 15},
            "BUY_ENABLED": True,
        }
        rules["SOL_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 5, "UP": 15},
            "BUY_ENABLED": True,
        }
        self.assertEqual(dca_config.maximum_daily_exposure_gbp(rules), 50)

    def test_requested_four_asset_policy_keeps_gbp_budgets_on_gbp_pairs(self):
        rules = {
            "BTC_GBP": {
                "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 20},
                "BUY_ENABLED": True,
            },
            "ETH_GBP": {
                "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 15},
                "BUY_ENABLED": True,
            },
            "SOL_GBP": {
                "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 15},
                "BUY_ENABLED": True,
            },
            "DOGE_GBP": {
                "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
                "BUY_ENABLED": False,
            },
        }
        validated = dca_config.validate_rules_map(rules)
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "DOWNTREND") for rule in validated.values()],
            [20, 15, 15, 0],
        )
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "SIDEWAYS") for rule in validated.values()],
            [10, 10, 10, 0],
        )
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "UPTREND") for rule in validated.values()],
            [5, 5, 5, 0],
        )

    def test_legacy_sideways_midpoint_uses_half_up_penny_rounding(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10.01, "UP": 10.02},
            "BUY_ENABLED": False,
        }
        self.assertEqual(dca_config.effective_amount_gbp(rule, "SIDEWAYS"), 10.02)

    def test_explicit_sideways_amount_is_not_derived(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        self.assertEqual(dca_config.effective_amount_gbp(rule, "SIDEWAYS"), 10)


class StateSchemaTests(unittest.TestCase):
    def test_ready_analysis_state_is_bound_to_current_rules(self):
        rules = dca_config.default_rules_map()
        state = ready_state(rules)
        validated = dca_config.validate_analysis_state(state, rules, now=NOW)
        self.assertEqual(set(validated["TARGETS"]), set(dca_config.ALLOWED_TARGETS))

        changed = copy.deepcopy(rules)
        changed["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        with self.assertRaisesRegex(ValueError, "does not match the live budgets"):
            dca_config.validate_analysis_state(state, changed)

    def test_analysis_schema_rejects_tier_mismatch_and_legacy_target(self):
        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["REGIME"] = "UPTREND"
        with self.assertRaisesRegex(ValueError, "does not match REGIME"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        for legacy_target in ("BTC_USD", "BTC_THB"):
            with self.subTest(legacy_target=legacy_target):
                state = ready_state()
                state["TARGETS"][legacy_target] = state["TARGETS"].pop("BTC_GBP")
                with self.assertRaisesRegex(
                    ValueError, f"unsupported targets.*{legacy_target}"
                ):
                    dca_config.validate_analysis_state(state)

        state = ready_state()
        state["VERSION"] = 1
        with self.assertRaisesRegex(ValueError, "VERSION must be 3"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_GBP"].update(
            {"REGIME": "UPTREND", "AMOUNT_TIER": "UP"}
        )
        with self.assertRaisesRegex(ValueError, "AMOUNT_TIER must be LOW, MID, or HIGH"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["VALID_UNTIL"] = "2026-08-06T02:01:00Z"
        with self.assertRaisesRegex(ValueError, "exactly 60 minutes"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["EXECUTE_AT"] = "2026-08-05T21:29:00Z"
        state["TARGETS"]["BTC_GBP"]["VALID_UNTIL"] = "2026-08-05T22:29:00Z"
        with self.assertRaisesRegex(ValueError, "match SELECTED_AT"):
            dca_config.validate_analysis_state(state)

    def test_error_decisions_are_complete_but_never_executable(self):
        state = dca_config.empty_analysis_state(now=NOW)
        validated = dca_config.validate_analysis_state(state)
        self.assertTrue(
            all(
                item["ANALYSIS_STATUS"] == "AWAITING_ANALYSIS"
                for item in validated["TARGETS"].values()
            )
        )
        usable, reason = dca_config.decision_is_usable(
            validated["TARGETS"]["BTC_GBP"],
            target="BTC_GBP",
            expected_rules_hash=validated["TARGETS"]["BTC_GBP"]["RULES_HASH"],
            now=NOW,
        )
        self.assertFalse(usable)
        self.assertIn("AWAITING_ANALYSIS", reason)

    def test_execution_window_is_inclusive_and_missed_decisions_are_stale(self):
        execute_at = "2026-08-06T01:00:00Z"
        self.assertTrue(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 0, 55, tzinfo=timezone.utc), execute_at
            )
        )
        self.assertTrue(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc), execute_at
            )
        )
        self.assertFalse(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 2, 0, 1, tzinfo=timezone.utc),
                execute_at,
                "2026-08-06T10:00:00Z",
            )
        )

        state = ready_state()
        decision = state["TARGETS"]["BTC_GBP"]
        usable, reason = dca_config.decision_is_usable(
            decision,
            target="BTC_GBP",
            expected_rules_hash=decision["RULES_HASH"],
            now=datetime(2026, 8, 6, 2, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(usable)
        self.assertIn("stale", reason)

    def test_decision_age_uses_per_target_analysis_timestamp(self):
        decision = ready_state()["TARGETS"]["BTC_GBP"]
        self.assertEqual(
            dca_config.decision_age_minutes(
                decision, datetime(2026, 8, 5, 21, 45, tzinfo=timezone.utc)
            ),
            45.0,
        )

    def test_pending_intent_requires_originating_decision_id(self):
        valid = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_ORDER": {
                    "client_order_id": "dca-0123456789abcd",
                    "funding_client_order_id": "dca-fedcba98765432",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-1",
                    "created_at": "2026-08-05T21:00:00Z",
                },
            }
        }
        self.assertEqual(
            dca_config.validate_execution_state(valid)["BTC_GBP"]["LAST_BUY_DATE"],
            "2026-08-05",
        )
        del valid["BTC_GBP"]["PENDING_ORDER"]["decision_id"]
        with self.assertRaisesRegex(ValueError, "decision_id"):
            dca_config.validate_execution_state(valid)

    def test_pending_intent_validates_every_durable_recovery_field(self):
        pending = {
            "client_order_id": "dca-0123456789abcd",
            "funding_client_order_id": "dca-fedcba98765432",
            "trade_date": "2026-08-05",
            "amount_gbp": 10,
            "decision_id": "decision-1",
            "created_at": "2026-08-05T21:00:00Z",
        }
        mutations = {
            "client_order_id": "wrong-id",
            "funding_client_order_id": "wrong-id",
            "trade_date": "2026-02-30",
            "amount_gbp": 4.99,
            "decision_id": "",
            "created_at": "2026-08-05T21:00:00",
        }
        for field, invalid in mutations.items():
            with self.subTest(field=field):
                candidate = copy.deepcopy(pending)
                candidate[field] = invalid
                with self.assertRaises(ValueError):
                    dca_config.validate_execution_state(
                        {"BTC_GBP": {"PENDING_ORDER": candidate}}
                    )

        duplicate_ids = copy.deepcopy(pending)
        duplicate_ids["funding_client_order_id"] = duplicate_ids["client_order_id"]
        with self.assertRaisesRegex(ValueError, "must differ from client_order_id"):
            dca_config.validate_execution_state(
                {"BTC_GBP": {"PENDING_ORDER": duplicate_ids}}
            )

        extra = copy.deepcopy(pending)
        extra["symbol"] = "BTC_GBP"
        with self.assertRaisesRegex(ValueError, "unsupported fields: symbol"):
            dca_config.validate_execution_state(
                {"BTC_GBP": {"PENDING_ORDER": extra}}
            )

    def test_pending_gist_deliveries_are_optional_fifo_and_empty_is_omitted(self):
        legacy = dca_config.validate_execution_state(
            {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-05"}}
        )
        self.assertEqual(legacy, {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-05"}})

        explicit_empty = dca_config.validate_execution_state(
            {"BTC_GBP": {"PENDING_GIST_DELIVERIES": []}}
        )
        self.assertEqual(explicit_empty, {"BTC_GBP": {"LAST_BUY_DATE": ""}})

        first = gist_delivery()
        second = gist_delivery(
            "OTWO22-SECOND-ORDER2", created_at="2026-08-06T01:06:00Z"
        )
        validated = dca_config.validate_execution_state(
            {"BTC_GBP": {"PENDING_GIST_DELIVERIES": [first, second]}}
        )
        self.assertEqual(
            validated["BTC_GBP"]["PENDING_GIST_DELIVERIES"],
            [first, second],
        )

    def test_gist_delivery_helper_enforces_exact_integrity_bound_schema(self):
        valid = gist_delivery()
        self.assertEqual(
            dca_config.validate_gist_delivery(valid, "BTC_GBP"),
            valid,
        )

        mutations = {
            "version": 1,
            "delivery_id": "unsafe order id",
            "created_at": "2026-08-06T01:05:00",
            "symbol": "SOL",
            "row": "not a markdown row\n",
            "row_sha256": "A" * 64,
        }
        for field, invalid in mutations.items():
            with self.subTest(field=field):
                candidate = copy.deepcopy(valid)
                candidate[field] = invalid
                with self.assertRaises(ValueError):
                    dca_config.validate_gist_delivery(candidate, "BTC_GBP")

        bool_version = copy.deepcopy(valid)
        bool_version["version"] = True
        with self.assertRaisesRegex(ValueError, "version must be 2"):
            dca_config.validate_gist_delivery(bool_version, "BTC_GBP")

        missing = copy.deepcopy(valid)
        del missing["created_at"]
        with self.assertRaisesRegex(ValueError, "missing: created_at"):
            dca_config.validate_gist_delivery(missing, "BTC_GBP")

        extra = {**valid, "attempts": 1}
        with self.assertRaisesRegex(ValueError, "unsupported fields: attempts"):
            dca_config.validate_gist_delivery(extra, "BTC_GBP")

        noncanonical_utc = gist_delivery(created_at="2026-08-06T01:05:00+00:00")
        with self.assertRaisesRegex(ValueError, "canonical UTC"):
            dca_config.validate_gist_delivery(noncanonical_utc, "BTC_GBP")

    def test_gist_delivery_row_is_one_bounded_utf8_line_with_matching_hash(self):
        for row in (
            "| first line |\n| second line |\n",
            "| carriage return |\r\n",
            "| missing newline |",
        ):
            with self.subTest(row=repr(row)):
                with self.assertRaisesRegex(ValueError, "one Markdown data line"):
                    dca_config.validate_gist_delivery(
                        gist_delivery(row=row), "BTC_GBP"
                    )

        delivery_id = "OUF4EM-FRGI2-MQMWZD"
        boundary_row = sized_gist_row(
            delivery_id, dca_config.MAX_GIST_DELIVERY_ROW_BYTES
        )
        dca_config.validate_gist_delivery(
            gist_delivery(delivery_id, row=boundary_row), "BTC_GBP"
        )
        oversized_row = sized_gist_row(
            delivery_id, dca_config.MAX_GIST_DELIVERY_ROW_BYTES + 1
        )
        with self.assertRaisesRegex(
            ValueError, f"at most {dca_config.MAX_GIST_DELIVERY_ROW_BYTES} UTF-8 bytes"
        ):
            dca_config.validate_gist_delivery(
                gist_delivery(delivery_id, row=oversized_row), "BTC_GBP"
            )

        mismatched = gist_delivery()
        mismatched["row_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match row"):
            dca_config.validate_gist_delivery(mismatched, "BTC_GBP")

        wrong_order_column = gist_delivery()
        wrong_order_column["row"] = wrong_order_column["row"].replace(
            wrong_order_column["delivery_id"], "DIFFERENT-ORDER"
        )
        wrong_order_column["row_sha256"] = sha256(
            wrong_order_column["row"].encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "crypto order column"):
            dca_config.validate_gist_delivery(wrong_order_column, "BTC_GBP")

        control_character = gist_delivery()
        control_character["row"] = control_character["row"].replace(
            "GBP 10.00", "GBP\t10.00"
        )
        control_character["row_sha256"] = sha256(
            control_character["row"].encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "control characters"):
            dca_config.validate_gist_delivery(control_character, "BTC_GBP")

    def test_pending_gist_queue_has_bounded_unique_delivery_ids(self):
        full_queue = [
            gist_delivery(f"ORDER-{index:02d}")
            for index in range(dca_config.MAX_PENDING_GIST_DELIVERIES)
        ]
        validated = dca_config.validate_execution_state(
            {"BTC_GBP": {"PENDING_GIST_DELIVERIES": full_queue}}
        )
        self.assertEqual(
            len(validated["BTC_GBP"]["PENDING_GIST_DELIVERIES"]),
            dca_config.MAX_PENDING_GIST_DELIVERIES,
        )

        with self.assertRaisesRegex(
            ValueError,
            f"at most {dca_config.MAX_PENDING_GIST_DELIVERIES} deliveries",
        ):
            dca_config.validate_execution_state(
                {
                    "BTC_GBP": {
                        "PENDING_GIST_DELIVERIES": full_queue
                        + [gist_delivery("ORDER-OVERFLOW")]
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "duplicate delivery_id"):
            dca_config.validate_execution_state(
                {
                    "BTC_GBP": {
                        "PENDING_GIST_DELIVERIES": [
                            gist_delivery("ORDER-DUPLICATE"),
                            gist_delivery("ORDER-DUPLICATE"),
                        ]
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "must be an array"):
            dca_config.validate_execution_state(
                {"BTC_GBP": {"PENDING_GIST_DELIVERIES": ()}}
            )

    def test_execution_state_global_json_budget_has_an_exact_boundary(self):
        state = {
            "BTC_GBP": {"PENDING_GIST_DELIVERIES": [gist_delivery()]}
        }
        normalized = dca_config.validate_execution_state(state)
        size = len(
            json.dumps(
                normalized, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )

        with patch.object(dca_config, "MAX_EXECUTION_STATE_JSON_BYTES", size):
            self.assertEqual(dca_config.validate_execution_state(state), normalized)
        with (
            patch.object(dca_config, "MAX_EXECUTION_STATE_JSON_BYTES", size - 1),
            self.assertRaisesRegex(ValueError, "size budget"),
        ):
            dca_config.validate_execution_state(state)

    def test_capacity_reserves_worst_case_delivery_for_every_pending_order(self):
        state = {
            "ETH_GBP": {
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "funding_client_order_id": "dca-fedcba09876543",
                    "trade_date": "2026-08-06",
                    "amount_gbp": 20.0,
                    "decision_id": "decision-eth",
                    "created_at": "2026-08-06T01:00:00Z",
                }
            }
        }
        normalized = dca_config.validate_execution_state(state)
        current_size = len(
            json.dumps(
                normalized, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        required = current_size + (2 * dca_config.GIST_DELIVERY_RESERVED_JSON_BYTES)
        with patch.object(dca_config, "MAX_EXECUTION_STATE_JSON_BYTES", required):
            dca_config.ensure_gist_delivery_capacity(state, "BTC_GBP")
        with (
            patch.object(dca_config, "MAX_EXECUTION_STATE_JSON_BYTES", required - 1),
            self.assertRaisesRegex(ValueError, "lacks reserved space"),
        ):
            dca_config.ensure_gist_delivery_capacity(state, "BTC_GBP")

    def test_capacity_rejects_a_full_target_delivery_queue(self):
        state = {
            "BTC_GBP": {
                "PENDING_GIST_DELIVERIES": [
                    gist_delivery(f"ORDER-{index:02d}")
                    for index in range(dca_config.MAX_PENDING_GIST_DELIVERIES)
                ]
            }
        }
        with self.assertRaisesRegex(ValueError, "No durable .* slot"):
            dca_config.ensure_gist_delivery_capacity(state, "BTC_GBP")

    def test_reserved_bytes_cover_maximum_valid_json_escaped_delivery(self):
        delivery_id = "O" * 128
        row = sized_gist_row(
            delivery_id, dca_config.MAX_GIST_DELIVERY_ROW_BYTES
        ).replace("x", '"')
        delivery = gist_delivery(delivery_id, row=row)
        dca_config.validate_gist_delivery(delivery, "BTC_GBP")
        baseline = {"BTC_GBP": {"LAST_BUY_DATE": ""}}
        with_delivery = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_GIST_DELIVERIES": [delivery],
            }
        }
        baseline_size = len(
            json.dumps(
                baseline, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        delivery_size = len(
            json.dumps(
                with_delivery, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        self.assertLessEqual(
            delivery_size - baseline_size,
            dca_config.GIST_DELIVERY_RESERVED_JSON_BYTES,
        )


if __name__ == "__main__":
    unittest.main()

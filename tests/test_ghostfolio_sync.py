import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).parents[1] / "ghostfolio" / "ghostfolio_sync.py"
SPEC = importlib.util.spec_from_file_location("ghostfolio_sync", PATH)
ghostfolio_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ghostfolio_sync)


def event(identifier="ORDER-1"):
    value = {
        "event_version": 3, "event_id": identifier,
        "occurred_at": "2026-08-06T01:00:00Z", "target": "SOL_GBP",
        "base_currency": "SOL", "quote_currency": "GBP", "budget_currency": "GBP",
        "funding_order_id": None, "crypto_order_id": identifier,
        "gbp_debit": "10", "gbp_usd_rate": "0", "funded_usd": "0",
        "route": "DIRECT_GBP", "crypto_cost_quote": "10", "crypto_quantity": "0.1",
        "unit_price_quote": "100", "funding_fee_quote": "0", "crypto_fee_quote": "0.03",
    }
    value["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(value).encode()
    ).hexdigest()
    return value


def resign_event(value):
    value.pop("canonical_hash", None)
    value["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(value).encode()
    ).hexdigest()
    return value


def yahoo_chart(*, start=None, closes=None):
    start = start or datetime(2026, 8, 1, 23, tzinfo=timezone.utc)
    closes = closes or [0.75, 0.751, 0.752, 0.753, 0.754, None]
    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {"currency": "GBP", "symbol": "GBP=X"},
                "timestamp": [
                    int((start + timedelta(days=index)).timestamp())
                    for index in range(len(closes))
                ],
                "indicators": {"quote": [{"close": closes}]},
            }],
        }
    }


def signed_snapshot():
    snapshot = {
        "version": 1,
        "as_of": "2026-08-07T04:00:00Z",
        "holdings": {
            "BTC_GBP": {
                "asset": "BTC",
                "pair": "BTC/GBP",
                "quantity": "0.2",
                "quote_currency": "GBP",
                "unit_price_quote": "50000",
            },
            "HYPE_USD": {
                "asset": "HYPE",
                "pair": "HYPE/USD",
                "quantity": "2",
                "quote_currency": "USD",
                "unit_price_quote": "40",
            },
            "SOL_GBP": {
                "asset": "SOL",
                "pair": "SOL/GBP",
                "quantity": "3",
                "quote_currency": "GBP",
                "unit_price_quote": "50",
            },
        },
        "unsupported_nonzero_assets": [],
    }
    snapshot["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(snapshot).encode()
    ).hexdigest()
    return snapshot


def resign_snapshot(snapshot):
    snapshot.pop("canonical_hash", None)
    snapshot["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(snapshot).encode()
    ).hexdigest()
    return snapshot


REPORTING_CONTEXT = {"kraken_account_id": "kraken"}
SNAPSHOT_NOW = datetime(2026, 8, 7, 4, 30, tzinfo=timezone.utc)


class GhostfolioSyncTests(unittest.TestCase):
    def setUp(self):
        self._intent_directory = tempfile.TemporaryDirectory()
        self._intent_patch = patch.object(
            ghostfolio_sync,
            "HOLDINGS_INTENT_PATH",
            Path(self._intent_directory.name) / "holdings-intent.json",
        )
        self._intent_patch.start()

    def tearDown(self):
        self._intent_patch.stop()
        self._intent_directory.cleanup()

    def test_compose_scopes_secrets_and_configuration_runs_one_shot(self):
        root = PATH.parents[1]
        compose = (root / "ghostfolio" / "compose.yml").read_text(
            encoding="utf-8"
        )
        for filename in ("app.env", "postgres.env", "redis.env", "sync.env"):
            self.assertIn(f"dca-ghostfolio/{filename}", compose)
        self.assertNotIn("DCA_GHOSTFOLIO_SECRETS_FILE", compose)

        configuration = (
            root / "ghostfolio" / "configure-local.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("run --rm --no-deps sync once", configuration)
        self.assertNotIn(
            "sync python /opt/sync/ghostfolio_sync.py once", configuration
        )
        self.assertIn("up -d --build --force-recreate sync", configuration)

        key_sync = (
            root / "ghostfolio" / "sync-canonical-key.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "$ghostfolioRoot = Join-Path $gladosRoot 'Ghostfolio'", key_sync
        )
        self.assertIn("$keyPath = Join-Path $ghostfolioRoot 'Key.txt'", key_sync)
        self.assertNotIn(
            "Join-Path (Split-Path $PSScriptRoot -Parent) 'Key.txt'", key_sync
        )

    def test_reporting_currency_is_not_rewritten_when_gbp_is_present(self):
        with patch.object(
            ghostfolio_sync,
            "request_json",
            return_value=(
                200,
                {"settings": {"CURRENCIES": ["GBP", "USD"]}},
            ),
        ) as request:
            currencies = ghostfolio_sync.ensure_ghostfolio_reporting_currency(
                "token"
            )

        self.assertEqual(currencies, ["GBP", "USD"])
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs, {"token": "token"})

    def test_reporting_currency_adds_gbp_once_and_reads_it_back(self):
        with patch.object(
            ghostfolio_sync,
            "request_json",
            side_effect=[
                (200, {"settings": {"CURRENCIES": ["USD"]}}),
                (200, {"key": "CURRENCIES"}),
                (200, {"settings": {"CURRENCIES": ["USD", "GBP"]}}),
            ],
        ) as request:
            currencies = ghostfolio_sync.ensure_ghostfolio_reporting_currency(
                "token"
            )

        self.assertEqual(currencies, ["USD", "GBP"])
        update = request.call_args_list[1]
        self.assertTrue(update.args[0].endswith("/api/v1/admin/settings/CURRENCIES"))
        self.assertEqual(update.kwargs["method"], "PUT")
        self.assertEqual(update.kwargs["payload"], {"value": '["USD","GBP"]'})

    def test_reporting_accounts_must_be_unique_and_gbp_denominated(self):
        response = {
            "accounts": [
                {"id": "kraken", "name": "Kraken DCA", "currency": "GBP"},
                {"id": "bitkub", "name": "Bitkub Legacy", "currency": "GBP"},
            ]
        }
        with patch.object(
            ghostfolio_sync, "request_json", return_value=(200, response)
        ):
            self.assertEqual(
                ghostfolio_sync.verify_ghostfolio_reporting_accounts("token"),
                {"Kraken DCA": "kraken", "Bitkub Legacy": "bitkub"},
            )

        response["accounts"][1]["currency"] = "USD"
        with (
            patch.object(
                ghostfolio_sync, "request_json", return_value=(200, response)
            ),
            self.assertRaisesRegex(RuntimeError, "Bitkub Legacy is not GBP"),
        ):
            ghostfolio_sync.verify_ghostfolio_reporting_accounts("token")

    def test_account_map_keeps_kraken_and_bitkub_custody_isolated(self):
        reporting_accounts = {
            "Kraken DCA": "kraken",
            "Bitkub Legacy": "bitkub",
        }
        valid_map = {
            "BTC_GBP": "kraken",
            "HYPE_USD": "kraken",
            "SOL_GBP": "kraken",
            "BITKUB_LEGACY": "bitkub",
        }
        with patch.dict(
            ghostfolio_sync.os.environ,
            {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps(valid_map)},
        ):
            self.assertEqual(
                ghostfolio_sync.verify_ghostfolio_account_map(
                    reporting_accounts
                ),
                "kraken",
            )

        invalid_map = dict(valid_map, SOL_GBP="bitkub")
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps(invalid_map)},
            ),
            self.assertRaisesRegex(RuntimeError, "invalid for: SOL_GBP"),
        ):
            ghostfolio_sync.verify_ghostfolio_account_map(reporting_accounts)

    def test_kraken_preflight_does_not_require_bitkub_but_full_reporting_is_distinct(self):
        kraken_only = {
            "accounts": [
                {"id": "kraken", "name": "Kraken DCA", "currency": "GBP"}
            ]
        }
        with patch.object(
            ghostfolio_sync, "request_json", return_value=(200, kraken_only)
        ):
            accounts = ghostfolio_sync.verify_ghostfolio_reporting_accounts(
                "token", require_bitkub=False
            )
        self.assertEqual(accounts, {"Kraken DCA": "kraken"})
        with patch.dict(
            ghostfolio_sync.os.environ,
            {
                "GHOSTFOLIO_ACCOUNT_MAP": json.dumps({
                    "BTC_GBP": "kraken",
                    "HYPE_USD": "kraken",
                    "SOL_GBP": "kraken",
                })
            },
        ):
            self.assertEqual(
                ghostfolio_sync.verify_ghostfolio_account_map(
                    accounts, require_bitkub=False
                ),
                "kraken",
            )

        same_account_id = {
            "accounts": [
                {"id": "shared", "name": "Kraken DCA", "currency": "GBP"},
                {"id": "shared", "name": "Bitkub Legacy", "currency": "GBP"},
            ]
        }
        with (
            patch.object(
                ghostfolio_sync,
                "request_json",
                return_value=(200, same_account_id),
            ),
            self.assertRaisesRegex(RuntimeError, "distinct IDs"),
        ):
            ghostfolio_sync.verify_ghostfolio_reporting_accounts("token")

    def test_quantity_audit_is_scoped_to_kraken_account(self):
        response = {
            "holdings": [{
                "assetProfile": {"symbol": "bitcoin"},
                "quantity": 0.25,
            }]
        }
        with patch.object(
            ghostfolio_sync, "request_json", return_value=(200, response)
        ) as request:
            quantities = ghostfolio_sync.ghostfolio_quantities(
                "token", "kraken-account"
            )

        self.assertEqual(quantities["BTC_GBP"], 0.25)
        self.assertIn("accounts=kraken-account", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["token"], "token")

    def test_yahoo_usdgbp_maps_timestamps_to_bangkok_utc_midnights(self):
        now = datetime(2026, 8, 7, 6, tzinfo=timezone.utc)
        with patch.object(
            ghostfolio_sync, "request_json", return_value=(200, yahoo_chart())
        ) as request:
            rows = ghostfolio_sync.yahoo_usdgbp_market_data(now=now)

        self.assertEqual(rows[0], {
            "date": "2026-08-02T00:00:00.000Z",
            "marketPrice": 0.75,
        })
        self.assertEqual(rows[-1]["date"], "2026-08-06T00:00:00.000Z")
        self.assertEqual(len(rows), 5)
        self.assertIn("query1.finance.yahoo.com", request.call_args.args[0])
        self.assertIn("USDGBP%3DX", request.call_args.args[0])

    def test_yahoo_usdgbp_fails_closed_on_missing_or_stale_data(self):
        malformed = yahoo_chart()
        malformed["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = "bad"
        with (
            patch.object(
                ghostfolio_sync, "request_json", return_value=(200, malformed)
            ),
            self.assertRaisesRegex(RuntimeError, "invalid close"),
        ):
            ghostfolio_sync.yahoo_usdgbp_market_data(
                now=datetime(2026, 8, 7, tzinfo=timezone.utc)
            )

        with (
            patch.object(
                ghostfolio_sync, "request_json", return_value=(200, yahoo_chart())
            ),
            self.assertRaisesRegex(RuntimeError, "stale or future-dated"),
        ):
            ghostfolio_sync.yahoo_usdgbp_market_data(
                now=datetime(2026, 9, 1, tzinfo=timezone.utc)
            )

    def test_usdgbp_rows_are_posted_to_supported_ghostfolio_endpoint(self):
        market_data = [
            {
                "date": f"2026-08-0{day}T00:00:00.000Z",
                "marketPrice": 0.75,
            }
            for day in range(1, 6)
        ]
        with patch.object(
            ghostfolio_sync,
            "request_json",
            return_value=(200, [{} for _ in market_data]),
        ) as request:
            count = ghostfolio_sync.publish_ghostfolio_usdgbp_market_data(
                "token", market_data
            )

        self.assertEqual(count, 5)
        self.assertTrue(
            request.call_args.args[0].endswith("/api/v1/market-data/YAHOO/USDGBP")
        )
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertEqual(request.call_args.kwargs["payload"], {"marketData": market_data})

    def test_fx_bridge_flushes_cache_after_upsert(self):
        order = []
        with (
            patch.object(
                ghostfolio_sync,
                "ensure_ghostfolio_reporting_currency",
                side_effect=lambda _token: order.append("currency"),
            ),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_reporting_accounts",
                side_effect=lambda _token: order.append("accounts") or {
                    "Kraken DCA": "kraken",
                    "Bitkub Legacy": "bitkub",
                },
            ),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_account_map",
                side_effect=lambda _accounts: order.append("map") or "kraken",
            ),
            patch.object(
                ghostfolio_sync,
                "yahoo_usdgbp_market_data",
                side_effect=lambda **_kwargs: order.append("fetch") or [{}] * 5,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_usdgbp_is_current",
                side_effect=lambda _token, **_kwargs: order.append("verify")
                or False,
            ),
            patch.object(
                ghostfolio_sync,
                "publish_ghostfolio_usdgbp_market_data",
                side_effect=lambda _token, _rows: order.append("upsert") or 5,
            ),
            patch.object(
                ghostfolio_sync,
                "flush_ghostfolio_cache",
                side_effect=lambda _token: order.append("flush"),
            ),
        ):
            result = ghostfolio_sync.prepare_ghostfolio_fx_bridge("token")

        self.assertEqual(
            result, {"fx_rows": 5, "kraken_account_id": "kraken"}
        )
        self.assertEqual(
            order,
            [
                "currency",
                "accounts",
                "map",
                "verify",
                "fetch",
                "upsert",
                "flush",
            ],
        )

    def test_fx_bridge_uses_current_stored_rate_without_yahoo(self):
        with (
            patch.object(
                ghostfolio_sync, "ensure_ghostfolio_reporting_currency"
            ),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_reporting_accounts",
                return_value={
                    "Kraken DCA": "kraken",
                    "Bitkub Legacy": "bitkub",
                },
            ),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_account_map",
                return_value="kraken",
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_usdgbp_is_current",
                return_value=True,
            ),
            patch.object(
                ghostfolio_sync, "yahoo_usdgbp_market_data"
            ) as yahoo,
            patch.object(
                ghostfolio_sync, "publish_ghostfolio_usdgbp_market_data"
            ) as publish,
            patch.object(ghostfolio_sync, "flush_ghostfolio_cache") as flush,
        ):
            result = ghostfolio_sync.prepare_ghostfolio_fx_bridge("token")

        self.assertEqual(
            result, {"fx_rows": 0, "kraken_account_id": "kraken"}
        )
        yahoo.assert_not_called()
        publish.assert_not_called()
        flush.assert_called_once_with("token")

    def test_stored_usdgbp_verification_accepts_a_recent_valid_rate(self):
        with patch.object(
            ghostfolio_sync,
            "request_json",
            side_effect=[
                (404, {}),
                (200, {"marketPrice": 0.7424}),
            ],
        ) as request:
            self.assertTrue(
                ghostfolio_sync.ghostfolio_usdgbp_is_current(
                    "token", now=datetime(2026, 8, 7, 6, tzinfo=timezone.utc)
                )
            )

        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args_list[1].args[0].endswith("2026-08-06"))

    def test_current_but_incomplete_fx_forces_backfill_and_recalculation(self):
        reporting = {"fx_rows": 0, "kraken_account_id": "kraken"}
        failed = {
            "portfolio_calculation_status": "ERROR",
            "portfolio_calculation_has_error": True,
        }
        repaired = {
            "portfolio_calculation_status": "OK",
            "portfolio_calculation_has_error": False,
        }
        with (
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                side_effect=[failed, repaired],
            ) as calculation,
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value={"fx_rows": 25, "kraken_account_id": "kraken"},
            ) as bridge,
        ):
            result = ghostfolio_sync.ghostfolio_portfolio_calculation_with_fx_repair(
                "token", reporting, now=SNAPSHOT_NOW
            )

        self.assertEqual(result, repaired)
        self.assertEqual(calculation.call_count, 2)
        bridge.assert_called_once_with(
            "token", now=SNAPSHOT_NOW, force_refresh=True
        )
        self.assertEqual(reporting["fx_rows"], 25)

    def test_fx_bridge_runs_before_portfolio_calculation_audit(self):
        order = []
        with (
            patch.object(ghostfolio_sync, "gist", return_value={"files": {}}),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                side_effect=lambda _token, **_kwargs: order.append("bridge")
                or REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                side_effect=lambda _token: order.append("audit") or {
                    "portfolio_calculation_status": "OK",
                    "portfolio_calculation_has_error": False,
                },
            ),
        ):
            result = ghostfolio_sync.reconcile_holdings_snapshot(token="token")

        self.assertEqual(order, ["bridge", "audit"])
        self.assertEqual(result["status"], "NO_SNAPSHOT")

    def test_portfolio_calculation_reports_ghostfolio_has_error(self):
        with patch.object(
            ghostfolio_sync,
            "request_json",
            return_value=(200, {"hasError": True}),
        ) as request:
            result = ghostfolio_sync.ghostfolio_portfolio_calculation("token")

        self.assertEqual(
            result,
            {
                "portfolio_calculation_status": "ERROR",
                "portfolio_calculation_has_error": True,
            },
        )
        self.assertEqual(request.call_args.kwargs["token"], "token")
        self.assertTrue(
            request.call_args.args[0].endswith(
                "/api/v1/portfolio/details?withMarkets=true"
            )
        )

    def test_sync_state_records_portfolio_calculation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with (
                patch.object(ghostfolio_sync, "STATE_PATH", state_path),
                patch.object(ghostfolio_sync, "gist", return_value={"files": {}}),
                patch.object(ghostfolio_sync, "ghostfolio_token", return_value="token"),
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_reporting_accounts",
                    return_value={
                        "Kraken DCA": "kraken",
                        "Bitkub Legacy": "bitkub",
                    },
                ),
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_account_map",
                    return_value="kraken",
                ),
                patch.object(
                    ghostfolio_sync,
                    "reconcile_holdings_snapshot",
                    return_value={
                        "status": "IN_SYNC",
                        "drift": {},
                        "snapshot_as_of": "2026-08-07T04:00:00Z",
                        "snapshot_hash": "a" * 64,
                        "portfolio_calculation_status": "ERROR",
                        "portfolio_calculation_has_error": True,
                    },
                ) as reconcile,
            ):
                ghostfolio_sync.sync_once()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["holdings_status"], "IN_SYNC")
            self.assertEqual(state["portfolio_calculation_status"], "ERROR")
            self.assertTrue(state["portfolio_calculation_has_error"])
            reconcile.assert_called_once_with(
                commit=True, token="token", gist_payload={"files": {}}
            )

    def test_sync_once_passes_one_immutable_gist_view_to_reconciliation(self):
        payload = {"files": {}}
        holdings = {
            "status": "IN_SYNC",
            "drift": {},
            "snapshot_as_of": "2026-08-07T04:00:00Z",
            "snapshot_hash": "a" * 64,
            "portfolio_calculation_status": "OK",
            "portfolio_calculation_has_error": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    ghostfolio_sync, "STATE_PATH", Path(directory) / "state.json"
                ),
                patch.object(
                    ghostfolio_sync, "gist", return_value=payload
                ) as gist_read,
                patch.object(
                    ghostfolio_sync, "ghostfolio_token", return_value="token"
                ),
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_reporting_accounts",
                    return_value={"Kraken DCA": "kraken"},
                ) as accounts,
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_account_map",
                    return_value="kraken",
                ) as account_map,
                patch.object(
                    ghostfolio_sync,
                    "reconcile_holdings_snapshot",
                    return_value=holdings,
                ) as reconcile,
            ):
                ghostfolio_sync.sync_once()

        gist_read.assert_called_once_with()
        accounts.assert_called_once_with("token", require_bitkub=False)
        account_map.assert_called_once_with(
            {"Kraken DCA": "kraken"}, require_bitkub=False
        )
        self.assertIs(reconcile.call_args.kwargs["gist_payload"], payload)

    def test_successful_event_import_without_activity_id_is_not_receipted(self):
        item = event("MISSING-ACTIVITY-ID")
        payload = {
            "files": {
                ghostfolio_sync.EVENT_FILE: {
                    "content": ghostfolio_sync.canonical(item) + "\n"
                }
            }
        }
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"SOL_GBP": "kraken"})},
            ),
            patch.object(ghostfolio_sync, "gist", return_value=payload),
            patch.object(ghostfolio_sync, "ghostfolio_token", return_value="token"),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_reporting_accounts",
                return_value={"Kraken DCA": "kraken"},
            ),
            patch.object(
                ghostfolio_sync,
                "verify_ghostfolio_account_map",
                return_value="kraken",
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                side_effect=[
                    (200, {}),
                    (201, {"activities": [{}]}),
                ],
            ),
            patch.object(ghostfolio_sync, "append_receipt") as append,
            patch.object(ghostfolio_sync, "reconcile_holdings_snapshot") as reconcile,
            self.assertRaisesRegex(RuntimeError, "acknowledgement is incomplete"),
        ):
            ghostfolio_sync.sync_once()

        append.assert_not_called()
        reconcile.assert_not_called()

    def test_old_holdings_intent_is_receipted_before_newer_event_import(self):
        old_snapshot = signed_snapshot()
        receipt_value = {
            "snapshot_hash": old_snapshot["canonical_hash"],
            "reconciled_at": "2026-08-07T04:15:00Z",
            "adjustments": [
                {"target": "BTC_GBP", "quantity_delta": "0.2"}
            ],
        }
        ghostfolio_sync.save_holdings_intent({
            "version": 1,
            "snapshot": old_snapshot,
            "receipt": receipt_value,
        })
        new_event = event("NEWER-SOL-FILL")
        new_event["occurred_at"] = "2026-08-07T04:30:00Z"
        resign_event(new_event)
        current_snapshot = json.loads(
            ghostfolio_sync.canonical(old_snapshot)
        )
        current_snapshot["as_of"] = "2026-08-07T05:00:00Z"
        current_snapshot["holdings"]["SOL_GBP"]["quantity"] = "3.1"
        resign_snapshot(current_snapshot)
        payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(current_snapshot)
                },
                ghostfolio_sync.EVENT_FILE: {
                    "content": ghostfolio_sync.canonical(new_event) + "\n"
                },
            }
        }
        order = []

        def publish_intent(*_args, **_kwargs):
            order.append("intent-receipt")
            return True

        def import_event(url, **_kwargs):
            order.append("event-import")
            if url.endswith("?dryRun=true"):
                return 200, {}
            return 201, {"activities": [{"id": "new-sol-activity"}]}

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    ghostfolio_sync.os.environ,
                    {
                        "GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"SOL_GBP": "kraken"}),
                        "HOLDINGS_SNAPSHOT_MAX_AGE_SECONDS": "14400",
                    },
                ),
                patch.object(
                    ghostfolio_sync, "STATE_PATH", Path(directory) / "state.json"
                ),
                patch.object(ghostfolio_sync, "gist", return_value=payload),
                patch.object(ghostfolio_sync, "ghostfolio_token", return_value="token"),
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_reporting_accounts",
                    return_value={"Kraken DCA": "kraken"},
                ),
                patch.object(
                    ghostfolio_sync,
                    "verify_ghostfolio_account_map",
                    return_value="kraken",
                ),
                patch.object(
                    ghostfolio_sync,
                    "prepare_ghostfolio_fx_bridge",
                    return_value=REPORTING_CONTEXT,
                ),
                patch.object(
                    ghostfolio_sync,
                    "ghostfolio_quantities",
                    side_effect=[
                        {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
                        {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3.1},
                    ],
                ),
                patch.object(
                    ghostfolio_sync,
                    "ghostfolio_portfolio_calculation_with_fx_repair",
                    return_value={
                        "portfolio_calculation_status": "OK",
                        "portfolio_calculation_has_error": False,
                    },
                ),
                patch.object(
                    ghostfolio_sync,
                    "append_named_receipt",
                    side_effect=publish_intent,
                ),
                patch.object(
                    ghostfolio_sync, "request_json", side_effect=import_event
                ),
                patch.object(
                    ghostfolio_sync, "append_receipt", return_value=True
                ),
            ):
                ghostfolio_sync.sync_once()
                state = json.loads(
                    (Path(directory) / "state.json").read_text(encoding="utf-8")
                )

        self.assertLess(order.index("intent-receipt"), order.index("event-import"))
        self.assertEqual(state["holdings_status"], "IN_SYNC")
        self.assertEqual(
            state["holdings_snapshot_hash"], current_snapshot["canonical_hash"]
        )
        self.assertFalse(ghostfolio_sync.HOLDINGS_INTENT_PATH.exists())

    def test_signed_snapshot_drift_is_reconciled_exactly_once(self):
        snapshot = signed_snapshot()
        post_import_order = []
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        quantities = [
            {"BTC_GBP": 0, "HYPE_USD": 2, "SOL_GBP": 3},
            {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
            {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
        ]

        def import_request(url, **_kwargs):
            if url.endswith("?dryRun=true"):
                return 200, {}
            if url.endswith("/api/v1/import"):
                return 201, {"activities": [{"id": "created"}]}
            raise AssertionError(f"unexpected URL {url}")

        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"BTC_GBP": "kraken"})},
            ),
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                side_effect=lambda _token: post_import_order.append("audit") or {
                    "portfolio_calculation_status": "OK",
                    "portfolio_calculation_has_error": False,
                },
            ),
            patch.object(
                ghostfolio_sync, "ghostfolio_quantities", side_effect=quantities
            ),
            patch.object(
                ghostfolio_sync, "request_json", side_effect=import_request
            ) as request,
            patch.object(
                ghostfolio_sync,
                "flush_ghostfolio_cache",
                side_effect=lambda _token: post_import_order.append("flush"),
            ) as flush,
            patch.object(
                ghostfolio_sync, "append_named_receipt", return_value=True
            ) as receipt,
        ):
            first = ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )
            second = ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        self.assertEqual(first["status"], "RECONCILED")
        self.assertEqual(second["status"], "IN_SYNC")
        self.assertEqual(request.call_count, 2)
        flush.assert_called_once_with("token")
        receipt.assert_called_once()
        self.assertEqual(post_import_order, ["flush", "audit", "audit"])

    def test_snapshot_receipt_is_blocked_until_quantities_converge(self):
        snapshot = signed_snapshot()
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        unchanged = {"BTC_GBP": 0, "HYPE_USD": 2, "SOL_GBP": 3}
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"BTC_GBP": "kraken"})},
            ),
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync, "ghostfolio_quantities", return_value=unchanged
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                side_effect=[
                    (200, {}),
                    (201, {"activities": [{"id": "created"}]}),
                ],
            ),
            patch.object(ghostfolio_sync, "flush_ghostfolio_cache") as flush,
            patch.object(ghostfolio_sync, "append_named_receipt") as receipt,
            self.assertRaisesRegex(RuntimeError, "did not converge for: BTC_GBP"),
        ):
            ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        flush.assert_called_once_with("token")
        receipt.assert_not_called()

    def test_missing_receipt_recovers_from_durable_local_intent(self):
        snapshot = signed_snapshot()
        receipt_value = {
            "snapshot_hash": snapshot["canonical_hash"],
            "reconciled_at": "2026-08-07T04:15:00Z",
            "adjustments": [
                {"target": "BTC_GBP", "quantity_delta": "0.2"}
            ],
        }
        ghostfolio_sync.save_holdings_intent({
            "version": 1,
            "snapshot": snapshot,
            "receipt": receipt_value,
        })
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        with (
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_quantities",
                return_value={"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                return_value={
                    "portfolio_calculation_status": "OK",
                    "portfolio_calculation_has_error": False,
                },
            ),
            patch.object(
                ghostfolio_sync, "append_named_receipt", return_value=True
            ) as append,
        ):
            result = ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        self.assertEqual(result["status"], "RECONCILED")
        append.assert_called_once()
        self.assertFalse(ghostfolio_sync.HOLDINGS_INTENT_PATH.exists())

    def test_durable_intent_finalizes_against_a_newer_matching_snapshot(self):
        old_snapshot = signed_snapshot()
        receipt_value = {
            "snapshot_hash": old_snapshot["canonical_hash"],
            "reconciled_at": "2026-08-07T04:15:00Z",
            "adjustments": [
                {"target": "BTC_GBP", "quantity_delta": "0.2"}
            ],
        }
        ghostfolio_sync.save_holdings_intent({
            "version": 1,
            "snapshot": old_snapshot,
            "receipt": receipt_value,
        })
        newer_snapshot = json.loads(
            ghostfolio_sync.canonical(old_snapshot)
        )
        newer_snapshot["as_of"] = "2026-08-07T05:00:00Z"
        resign_snapshot(newer_snapshot)
        payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(newer_snapshot)
                }
            }
        }
        calculation = {
            "portfolio_calculation_status": "OK",
            "portfolio_calculation_has_error": False,
        }
        with (
            patch.object(
                ghostfolio_sync,
                "STATE_PATH",
                Path(self._intent_directory.name) / "state.json",
            ),
            patch.object(ghostfolio_sync, "gist", return_value=payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_quantities",
                return_value={"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation_with_fx_repair",
                return_value=calculation,
            ),
            patch.object(
                ghostfolio_sync, "append_named_receipt", return_value=True
            ) as append,
        ):
            result = ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True,
                token="token",
                now=datetime(2026, 8, 7, 5, 30, tzinfo=timezone.utc),
                gist_payload=payload,
            )

        self.assertEqual(result["status"], "RECONCILED")
        self.assertEqual(append.call_args.args[3], receipt_value)
        self.assertFalse(ghostfolio_sync.HOLDINGS_INTENT_PATH.exists())

    def test_durable_intent_can_resume_after_same_snapshot_becomes_stale(self):
        snapshot = signed_snapshot()
        receipt_value = {
            "snapshot_hash": snapshot["canonical_hash"],
            "reconciled_at": "2026-08-07T04:15:00Z",
            "adjustments": [
                {"target": "BTC_GBP", "quantity_delta": "0.2"}
            ],
        }
        ghostfolio_sync.save_holdings_intent({
            "version": 1,
            "snapshot": snapshot,
            "receipt": receipt_value,
        })
        payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"BTC_GBP": "kraken"})},
            ),
            patch.object(
                ghostfolio_sync,
                "STATE_PATH",
                Path(self._intent_directory.name) / "state.json",
            ),
            patch.object(ghostfolio_sync, "gist", return_value=payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_quantities",
                side_effect=[
                    {"BTC_GBP": 0, "HYPE_USD": 2, "SOL_GBP": 3},
                    {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
                ],
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                side_effect=[
                    (200, {}),
                    (201, {"activities": [{"id": "created"}]}),
                ],
            ),
            patch.object(ghostfolio_sync, "flush_ghostfolio_cache") as flush,
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation_with_fx_repair",
                return_value={
                    "portfolio_calculation_status": "OK",
                    "portfolio_calculation_has_error": False,
                },
            ),
            patch.object(
                ghostfolio_sync, "append_named_receipt", return_value=True
            ) as append,
        ):
            result = ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True,
                token="token",
                now=datetime(2026, 8, 7, 7, tzinfo=timezone.utc),
                gist_payload=payload,
            )

        self.assertEqual(result["status"], "RECONCILED")
        flush.assert_called_once_with("token")
        append.assert_called_once()
        self.assertFalse(ghostfolio_sync.HOLDINGS_INTENT_PATH.exists())

    def test_calculation_error_retains_intent_and_blocks_receipt(self):
        snapshot = signed_snapshot()
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        quantities = [
            {"BTC_GBP": 0, "HYPE_USD": 2, "SOL_GBP": 3},
            {"BTC_GBP": 0.2, "HYPE_USD": 2, "SOL_GBP": 3},
        ]
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"BTC_GBP": "kraken"})},
            ),
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync, "ghostfolio_quantities", side_effect=quantities
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                side_effect=[
                    (200, {}),
                    (201, {"activities": [{"id": "created"}]}),
                ],
            ),
            patch.object(ghostfolio_sync, "flush_ghostfolio_cache"),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                return_value={
                    "portfolio_calculation_status": "ERROR",
                    "portfolio_calculation_has_error": True,
                },
            ),
            patch.object(ghostfolio_sync, "append_named_receipt") as append,
            self.assertRaisesRegex(RuntimeError, "calculation is incomplete"),
        ):
            ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        append.assert_not_called()
        self.assertTrue(ghostfolio_sync.HOLDINGS_INTENT_PATH.is_file())

    def test_signed_snapshot_reconciliation_conflict_fails_closed(self):
        snapshot = signed_snapshot()
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"BTC_GBP": "kraken"})},
            ),
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(
                ghostfolio_sync,
                "prepare_ghostfolio_fx_bridge",
                return_value=REPORTING_CONTEXT,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_portfolio_calculation",
                return_value={
                    "portfolio_calculation_status": "OK",
                    "portfolio_calculation_has_error": False,
                },
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_quantities",
                return_value={"BTC_GBP": 0, "HYPE_USD": 2, "SOL_GBP": 3},
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                return_value=(400, {"message": ["currency conflict"]}),
            ) as request,
            patch.object(ghostfolio_sync, "append_named_receipt") as receipt,
            self.assertRaisesRegex(RuntimeError, "dry-run conflict for BTC_GBP"),
        ):
            ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        request.assert_called_once()
        receipt.assert_not_called()

    def test_snapshot_older_than_portfolio_event_cannot_adjust_holdings(self):
        snapshot = signed_snapshot()
        newer_event = event("NEWER-FILL")
        newer_event["occurred_at"] = "2026-08-08T01:00:00Z"
        newer_event.pop("canonical_hash")
        newer_event["canonical_hash"] = hashlib.sha256(
            ghostfolio_sync.canonical(newer_event).encode()
        ).hexdigest()
        gist_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                },
                ghostfolio_sync.EVENT_FILE: {
                    "content": ghostfolio_sync.canonical(newer_event) + "\n"
                },
            }
        }
        with (
            patch.object(ghostfolio_sync, "gist", return_value=gist_payload),
            patch.object(ghostfolio_sync, "prepare_ghostfolio_fx_bridge") as bridge,
            patch.object(ghostfolio_sync, "request_json") as request,
            patch.object(ghostfolio_sync, "append_named_receipt") as receipt,
            self.assertRaisesRegex(
                RuntimeError, "snapshot predates portfolio event NEWER-FILL"
            ),
        ):
            ghostfolio_sync.reconcile_holdings_snapshot(
                commit=True, token="token", now=SNAPSHOT_NOW
            )

        bridge.assert_not_called()
        request.assert_not_called()
        receipt.assert_not_called()

    def test_snapshot_and_event_timestamps_require_timezone_aware_iso(self):
        snapshot = signed_snapshot()
        with self.assertRaisesRegex(RuntimeError, "timestamp has no timezone"):
            ghostfolio_sync.validate_snapshot_event_order(
                snapshot,
                [{"event_id": "BAD-TIME", "occurred_at": "2026-08-07T05:00:00"}],
            )

    def test_snapshot_freshness_and_monotonicity_fail_closed(self):
        snapshot = signed_snapshot()
        self.assertEqual(
            ghostfolio_sync.validate_snapshot_freshness(
                snapshot, now=SNAPSHOT_NOW
            ),
            datetime(2026, 8, 7, 4, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(RuntimeError, "snapshot is stale"):
            ghostfolio_sync.validate_snapshot_freshness(
                snapshot,
                now=datetime(2026, 8, 7, 7, tzinfo=timezone.utc),
            )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps({
                    "holdings_snapshot_as_of": "2026-08-07T04:01:00Z",
                    "holdings_snapshot_hash": "a" * 64,
                }),
                encoding="utf-8",
            )
            with (
                patch.object(ghostfolio_sync, "STATE_PATH", state_path),
                self.assertRaisesRegex(RuntimeError, "moved backwards"),
            ):
                ghostfolio_sync.validate_snapshot_monotonicity(snapshot)

    def test_health_fails_for_calculation_error_or_quantity_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with (
                patch.object(ghostfolio_sync, "STATE_PATH", state_path),
                patch.object(ghostfolio_sync.sys, "argv", ["ghostfolio_sync.py", "health"]),
            ):
                fresh_snapshot = datetime.now(timezone.utc).isoformat()
                healthy_fields = {
                    "holdings_snapshot_as_of": fresh_snapshot,
                    "holdings_snapshot_hash": "a" * 64,
                }
                state_path.write_text(
                    json.dumps({
                        "holdings_status": "IN_SYNC",
                        "portfolio_calculation_has_error": True,
                        **healthy_fields,
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(ghostfolio_sync.main(), 1)

                state_path.write_text(
                    json.dumps({
                        "holdings_status": "DRIFT",
                        "portfolio_calculation_has_error": False,
                        **healthy_fields,
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(ghostfolio_sync.main(), 1)

                state_path.write_text(
                    json.dumps({
                        "holdings_status": "IN_SYNC",
                        "portfolio_calculation_has_error": False,
                        **healthy_fields,
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(ghostfolio_sync.main(), 0)

                # The prior state schema cannot prove current Kraken coverage.
                state_path.write_text(
                    json.dumps({"holdings_status": "IN_SYNC"}),
                    encoding="utf-8",
                )
                self.assertEqual(ghostfolio_sync.main(), 1)

    def test_snapshot_rejects_nonfinite_values_and_wrong_pair_identity(self):
        cases = [
            ("quantity", "NaN", "quantity"),
            ("price", "Infinity", "price"),
            ("identity", "SOL/USD", "identity"),
        ]
        for label, value, message in cases:
            with self.subTest(label=label):
                snapshot = json.loads(ghostfolio_sync.canonical(signed_snapshot()))
                if label == "quantity":
                    snapshot["holdings"]["BTC_GBP"]["quantity"] = value
                elif label == "price":
                    snapshot["holdings"]["HYPE_USD"]["unit_price_quote"] = value
                else:
                    snapshot["holdings"]["SOL_GBP"]["pair"] = value
                resign_snapshot(snapshot)
                with self.assertRaisesRegex(RuntimeError, message):
                    ghostfolio_sync.parse_holdings_snapshot(
                        ghostfolio_sync.canonical(snapshot)
                    )

    def test_actual_ghostfolio_quantities_reject_nonfinite_or_missing_values(self):
        snapshot = signed_snapshot()
        for actual in (
            {"BTC_GBP": float("nan"), "HYPE_USD": 2, "SOL_GBP": 3},
            {"BTC_GBP": 0.2, "HYPE_USD": float("inf"), "SOL_GBP": 3},
            {"BTC_GBP": 0.2, "HYPE_USD": 2},
        ):
            with self.subTest(actual=actual):
                with self.assertRaisesRegex(RuntimeError, "holding quantity"):
                    ghostfolio_sync.holdings_drift(snapshot, actual)

        with (
            patch.object(
                ghostfolio_sync,
                "request_json",
                return_value=(200, {
                    "holdings": [{
                        "assetProfile": {"symbol": "bitcoin"},
                        "quantity": float("inf"),
                    }]
                }),
            ),
            self.assertRaisesRegex(RuntimeError, "holding quantity for BTC_GBP"),
        ):
            ghostfolio_sync.ghostfolio_quantities("token", "kraken")

    def test_signed_holdings_snapshot_and_drift(self):
        snapshot = {
            "version": 1,
            "as_of": "2026-08-07T04:00:00Z",
            "holdings": {
                "BTC_GBP": {"asset": "BTC", "pair": "BTC/GBP", "quantity": "0.2", "quote_currency": "GBP", "unit_price_quote": "50000"},
                "HYPE_USD": {"asset": "HYPE", "pair": "HYPE/USD", "quantity": "2", "quote_currency": "USD", "unit_price_quote": "40"},
                "SOL_GBP": {"asset": "SOL", "pair": "SOL/GBP", "quantity": "3", "quote_currency": "GBP", "unit_price_quote": "50"},
            },
            "unsupported_nonzero_assets": [],
        }
        snapshot["canonical_hash"] = hashlib.sha256(
            ghostfolio_sync.canonical(snapshot).encode()
        ).hexdigest()
        parsed = ghostfolio_sync.parse_holdings_snapshot(
            ghostfolio_sync.canonical(snapshot)
        )
        self.assertEqual(
            ghostfolio_sync.holdings_drift(
                parsed, {"BTC_GBP": 0.1, "HYPE_USD": 2, "SOL_GBP": 3}
            ),
            {"BTC_GBP": 0.1},
        )
        prior = ghostfolio_sync.os.environ.get("GHOSTFOLIO_ACCOUNT_MAP")
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps(
            {"BTC_GBP": "kraken-account"}
        )
        try:
            activity = ghostfolio_sync.holdings_import_payload(
                parsed, "BTC_GBP", 0.1
            )
            self.assertEqual(activity["activities"][0]["type"], "BUY")
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior

    def test_holdings_snapshot_blocks_unmapped_assets(self):
        snapshot = {
            "version": 1,
            "as_of": "2026-08-07T04:00:00Z",
            "holdings": {},
            "unsupported_nonzero_assets": ["ETH"],
        }
        snapshot["canonical_hash"] = hashlib.sha256(
            ghostfolio_sync.canonical(snapshot).encode()
        ).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "without a Ghostfolio mapping"):
            ghostfolio_sync.parse_holdings_snapshot(
                ghostfolio_sync.canonical(snapshot)
            )

    def test_hash_chain_and_duplicate_event_ids_are_enforced(self):
        first = event()
        content = ghostfolio_sync.canonical(first) + "\n"
        self.assertEqual(ghostfolio_sync.parse_events(content), [first])
        corrupt = dict(first, crypto_quantity="0.2")
        with self.assertRaisesRegex(RuntimeError, "invalid append-only hash"):
            ghostfolio_sync.parse_events(ghostfolio_sync.canonical(corrupt))
        with self.assertRaisesRegex(RuntimeError, "duplicates"):
            ghostfolio_sync.parse_events(content + content)

    def test_portfolio_event_v3_schema_is_strict(self):
        cases = []
        missing = event()
        missing.pop("crypto_fee_quote")
        cases.append(missing)
        extra = event()
        extra["unexpected"] = "value"
        cases.append(extra)
        wrong_version = event()
        wrong_version["event_version"] = 2
        cases.append(wrong_version)
        invalid_id = event()
        invalid_id["event_id"] = ""
        invalid_id["crypto_order_id"] = ""
        cases.append(invalid_id)
        bad_time = event()
        bad_time["occurred_at"] = "2026-08-06T08:00:00+07:00"
        cases.append(bad_time)
        wrong_route = event()
        wrong_route["route"] = "GBP_TO_USD"
        cases.append(wrong_route)
        nonfinite = event()
        nonfinite["crypto_quantity"] = "NaN"
        cases.append(nonfinite)
        negative = event()
        negative["crypto_fee_quote"] = "-0.01"
        cases.append(negative)
        zero_price = event()
        zero_price["unit_price_quote"] = "0"
        cases.append(zero_price)

        for candidate in cases:
            with self.subTest(candidate=candidate):
                resign_event(candidate)
                with self.assertRaises(RuntimeError):
                    ghostfolio_sync.parse_events(
                        ghostfolio_sync.canonical(candidate) + "\n"
                    )

    def test_event_receipt_hash_must_match_the_signed_event(self):
        item = event()
        receipt = {
            "order_id": item["event_id"],
            "event_hash": item["canonical_hash"],
            "ghostfolio_activity_id": "activity-1",
            "imported_at": "2026-08-07T04:00:00Z",
        }
        content = ghostfolio_sync.canonical(receipt) + "\n"
        self.assertEqual(
            ghostfolio_sync.parse_event_receipts(content, [item]),
            {item["event_id"]},
        )
        receipt["event_hash"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            ghostfolio_sync.parse_event_receipts(
                ghostfolio_sync.canonical(receipt), [item]
            )

    def test_only_exact_duplicate_response_is_accepted(self):
        self.assertTrue(
            ghostfolio_sync.is_exact_duplicate(
                {"message": ["activities.0 is a duplicate activity"]}
            )
        )
        self.assertFalse(ghostfolio_sync.is_exact_duplicate({"message": ["currency conflict"]}))

    def test_import_payload_uses_local_custody_account_and_separate_fee_comment(self):
        prior = ghostfolio_sync.os.environ.get("GHOSTFOLIO_ACCOUNT_MAP")
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps({"SOL_GBP": "local-sol"})
        try:
            activity = ghostfolio_sync.import_payload(event())["activities"][0]
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior
        self.assertEqual(activity["accountId"], "local-sol")
        self.assertEqual(activity["symbol"], "solana")
        self.assertIn("funding fee GBP 0", activity["comment"])
        self.assertIn("crypto fee GBP 0.03", activity["comment"])

    def test_hype_uses_supported_ghostfolio_yahoo_profile(self):
        hype = event()
        hype["target"] = "HYPE_USD"
        prior = ghostfolio_sync.os.environ.get("GHOSTFOLIO_ACCOUNT_MAP")
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps(
            {"HYPE_USD": "local-hype"}
        )
        try:
            activity = ghostfolio_sync.import_payload(hype)["activities"][0]
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior
        self.assertEqual(activity["dataSource"], "YAHOO")
        self.assertEqual(activity["symbol"], "HYPE32196USD")


if __name__ == "__main__":
    unittest.main()

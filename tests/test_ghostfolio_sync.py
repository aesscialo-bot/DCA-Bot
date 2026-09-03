import base64
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

SYNTHETIC_RECOVERY_EVENT_ID = "OTEST1-CRYPTO-ORDER"
SYNTHETIC_RECOVERY_FUNDING_ID = "OTEST1-FUNDING-ORDER"
SYNTHETIC_OPENING_HASH = "b" * 64
SYNTHETIC_OPENING_QUANTITY = 1.25
SYNTHETIC_EVENT_QUANTITY = 0.25
SYNTHETIC_RESIDUAL_QUANTITY = 1.0
SYNTHETIC_RECOVERY_COMPLETED_AT = "2026-08-07T08:30:00Z"


def repository_environment():
    return {
        ghostfolio_sync.REPOSITORY_OWNER_ENV: "owner",
        ghostfolio_sync.REPOSITORY_NAME_ENV: "canonical-ledger",
        ghostfolio_sync.REPOSITORY_BRANCH_ENV: "main",
        ghostfolio_sync.REPOSITORY_TOKEN_ENV: "private-token",
        ghostfolio_sync.REPOSITORY_EVENT_PATH_ENV: (
            "portfolio/kraken_usd_dca_ghostfolio_events.jsonl"
        ),
        ghostfolio_sync.REPOSITORY_HOLDINGS_PATH_ENV: (
            "portfolio/kraken_holdings_snapshot_v1.json"
        ),
        ghostfolio_sync.REPOSITORY_EVENT_RECEIPT_PATH_ENV: (
            "portfolio/ghostfolio_sync_receipts.jsonl"
        ),
        ghostfolio_sync.REPOSITORY_HOLDINGS_RECEIPT_PATH_ENV: (
            "portfolio/ghostfolio_holdings_receipts.jsonl"
        ),
        ghostfolio_sync.REPOSITORY_PROVENANCE_RECEIPT_PATH_ENV: (
            "portfolio/ghostfolio_provenance_reclassification_receipts.jsonl"
        ),
    }


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


def current_signed_snapshot():
    snapshot = signed_snapshot()
    snapshot.pop("canonical_hash")
    snapshot["version"] = 2
    snapshot["holdings"]["ETH_GBP"] = {
        "asset": "ETH",
        "pair": "ETH/GBP",
        "quantity": "0.4",
        "quote_currency": "GBP",
        "unit_price_quote": "2500",
    }
    return resign_snapshot(snapshot)


def resign_snapshot(snapshot):
    snapshot.pop("canonical_hash", None)
    snapshot["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(snapshot).encode()
    ).hexdigest()
    return snapshot


def confirmed_hype_event():
    value = {
        "event_version": 3,
        "event_id": SYNTHETIC_RECOVERY_EVENT_ID,
        "occurred_at": "2026-08-07T06:02:03Z",
        "target": "HYPE_USD",
        "base_currency": "HYPE",
        "quote_currency": "USD",
        "budget_currency": "GBP",
        "funding_order_id": SYNTHETIC_RECOVERY_FUNDING_ID,
        "crypto_order_id": SYNTHETIC_RECOVERY_EVENT_ID,
        "gbp_debit": "9",
        "gbp_usd_rate": "1.25",
        "funded_usd": "11.25",
        "route": "GBP_TO_USD",
        "crypto_cost_quote": "11",
        "crypto_quantity": "0.25",
        "unit_price_quote": "44",
        "funding_fee_quote": "0.02",
        "crypto_fee_quote": "0.03",
    }
    return resign_event(value)


def synthetic_recovery_snapshot():
    snapshot = {
        "version": 1,
        "as_of": "2026-08-07T08:00:00Z",
        "holdings": {
            "BTC_GBP": {
                "asset": "BTC",
                "pair": "BTC/GBP",
                "quantity": "0.01",
                "quote_currency": "GBP",
                "unit_price_quote": "50000",
            },
            "HYPE_USD": {
                "asset": "HYPE",
                "pair": "HYPE/USD",
                "quantity": "1.25",
                "quote_currency": "USD",
                "unit_price_quote": "40",
            },
            "SOL_GBP": {
                "asset": "SOL",
                "pair": "SOL/GBP",
                "quantity": "2",
                "quote_currency": "GBP",
                "unit_price_quote": "50",
            },
        },
        "unsupported_nonzero_assets": [],
    }
    return resign_snapshot(snapshot)


def hype_opening_activity(*, residual=False):
    return {
        "id": "hype-opening-activity",
        "accountId": "kraken",
        "comment": (
            ghostfolio_sync._recovery_residual_comment(
                SYNTHETIC_OPENING_HASH, SYNTHETIC_RECOVERY_EVENT_ID
            )
            if residual
            else (
                "Kraken opening-balance reconciliation; "
                f"snapshot={SYNTHETIC_OPENING_HASH}; target=HYPE_USD"
            )
        ),
        "currency": "USD",
        "date": "2026-08-07T06:30:00Z",
        "fee": 0,
        "quantity": (
            SYNTHETIC_RESIDUAL_QUANTITY
            if residual
            else SYNTHETIC_OPENING_QUANTITY
        ),
        "type": "BUY",
        "unitPrice": 40,
        "tags": [],
        "assetProfile": {"dataSource": "YAHOO", "symbol": "HYPE32196USD"},
    }


def hype_event_activity(item=None):
    item = item or confirmed_hype_event()
    return {
        "id": "hype-recovery-activity",
        "accountId": "kraken",
        "comment": (
            f"Kraken orders funding={item['funding_order_id']} "
            f"crypto={item['crypto_order_id']}; route={item['route']}; "
            f"funding fee {item['quote_currency']} {item['funding_fee_quote']}; "
            f"crypto fee {item['quote_currency']} {item['crypto_fee_quote']}"
        ),
        "currency": "USD",
        "date": item["occurred_at"],
        "fee": float(item["crypto_fee_quote"]),
        "quantity": float(item["crypto_quantity"]),
        "type": "BUY",
        "unitPrice": float(item["unit_price_quote"]),
        "tags": [],
        "assetProfile": {
            "dataSource": "YAHOO",
            "symbol": "HYPE32196USD",
        },
    }


def hype_recovery_payload():
    item = confirmed_hype_event()
    snapshot = synthetic_recovery_snapshot()
    source_receipt = {
        "snapshot_hash": SYNTHETIC_OPENING_HASH,
        "reconciled_at": "2026-08-07T06:35:00Z",
        "adjustments": [
            {"target": "HYPE_USD", "quantity_delta": "1.25"},
            {"target": "SOL_GBP", "quantity_delta": "0.5"},
        ],
    }
    return {
        "files": {
            ghostfolio_sync.EVENT_FILE: {
                "content": ghostfolio_sync.canonical(item) + "\n"
            },
            ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                "content": ghostfolio_sync.canonical(snapshot)
            },
            ghostfolio_sync.HOLDINGS_RECEIPT_FILE: {
                "content": ghostfolio_sync.canonical(source_receipt) + "\n"
            },
        }
    }


def completed_hype_recovery_payload():
    payload = hype_recovery_payload()
    item = confirmed_hype_event()
    payload["files"][ghostfolio_sync.RECEIPT_FILE] = {
        "content": ghostfolio_sync.canonical({
            "order_id": item["event_id"],
            "event_hash": item["canonical_hash"],
            "ghostfolio_activity_id": "hype-recovery-activity",
            "imported_at": SYNTHETIC_RECOVERY_COMPLETED_AT,
        }) + "\n"
    }
    payload["files"][ghostfolio_sync.PROVENANCE_RECLASSIFICATION_RECEIPT_FILE] = {
        "content": ghostfolio_sync.canonical({
            "version": 1,
            "event_id": item["event_id"],
            "event_hash": item["canonical_hash"],
            "opening_snapshot_hash": SYNTHETIC_OPENING_HASH,
            "opening_activity_id": "hype-opening-activity",
            "original_quantity": "1.25",
            "residual_quantity": "1",
            "reclassified_quantity": "0.25",
            "completed_at": SYNTHETIC_RECOVERY_COMPLETED_AT,
        }) + "\n"
    }
    return payload


REPORTING_CONTEXT = {"kraken_account_id": "kraken"}
SNAPSHOT_NOW = datetime(2026, 8, 7, 4, 30, tzinfo=timezone.utc)


class GhostfolioSyncTests(unittest.TestCase):
    def setUp(self):
        self._intent_directory = tempfile.TemporaryDirectory()
        self._recovery_env_patch = patch.dict(
            ghostfolio_sync.os.environ,
            {
                ghostfolio_sync.HYPE_RECOVERY_EVENT_ID_ENV: (
                    SYNTHETIC_RECOVERY_EVENT_ID
                ),
                ghostfolio_sync.HYPE_RECOVERY_FUNDING_ORDER_ID_ENV: (
                    SYNTHETIC_RECOVERY_FUNDING_ID
                ),
                ghostfolio_sync.HYPE_RECOVERY_EVENT_HASH_ENV: (
                    confirmed_hype_event()["canonical_hash"]
                ),
            },
        )
        self._intent_patch = patch.object(
            ghostfolio_sync,
            "HOLDINGS_INTENT_PATH",
            Path(self._intent_directory.name) / "holdings-intent.json",
        )
        self._provenance_intent_patch = patch.object(
            ghostfolio_sync,
            "PROVENANCE_RECLASSIFICATION_INTENT_PATH",
            Path(self._intent_directory.name) / "provenance-intent.json",
        )
        self._intent_patch.start()
        self._provenance_intent_patch.start()
        self._recovery_env_patch.start()

    def tearDown(self):
        self._recovery_env_patch.stop()
        self._provenance_intent_patch.stop()
        self._intent_patch.stop()
        self._intent_directory.cleanup()

    def _process_completed_hype_reclassification(self, activities, payload=None):
        if payload is None:
            payload = completed_hype_recovery_payload()
        events = ghostfolio_sync.parse_events(
            ghostfolio_sync.file_content(payload, ghostfolio_sync.EVENT_FILE)
        )
        receipts = ghostfolio_sync.parse_event_receipts(
            ghostfolio_sync.file_content(payload, ghostfolio_sync.RECEIPT_FILE),
            events,
        )
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"HYPE_USD": "kraken"})},
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_hype_activities",
                return_value=activities,
            ),
        ):
            return ghostfolio_sync.process_hype_provenance_reclassification(
                payload, events, receipts, "token", "kraken"
            )

    def test_repository_snapshot_reads_every_artifact_at_one_commit(self):
        environment = repository_environment()
        commit_sha = "a" * 40
        contents = {
            environment[setting]: f"content for {filename}\n"
            for filename, setting in (
                ghostfolio_sync.REPOSITORY_PATH_ENV_BY_FILE.items()
            )
        }
        calls = []

        def github_response(url, **kwargs):
            calls.append((url, kwargs))
            self.assertEqual(kwargs["token"], "private-token")
            self.assertEqual(
                kwargs["request_headers"]["X-GitHub-Api-Version"],
                ghostfolio_sync.GITHUB_API_VERSION,
            )
            if url.endswith("/repos/owner/canonical-ledger"):
                return 200, {
                    "private": True,
                    "full_name": "owner/canonical-ledger",
                }
            if url.endswith("/commits/main"):
                return 200, {"sha": commit_sha}
            for path, content in contents.items():
                if f"/contents/{path}?" in url:
                    raw = content.encode("utf-8")
                    return 200, {
                        "type": "file",
                        "path": path,
                        "sha": "b" * 40,
                        "size": len(raw),
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode("ascii"),
                    }
            self.fail(f"unexpected private repository request: {url}")

        with (
            patch.dict(ghostfolio_sync.os.environ, environment),
            patch.object(
                ghostfolio_sync, "request_json", side_effect=github_response
            ),
        ):
            snapshot = ghostfolio_sync.repository_snapshot()

        self.assertEqual(snapshot["repository_commit_sha"], commit_sha)
        self.assertEqual(
            set(snapshot["files"]),
            set(ghostfolio_sync.REPOSITORY_PATH_ENV_BY_FILE),
        )
        for filename, setting in (
            ghostfolio_sync.REPOSITORY_PATH_ENV_BY_FILE.items()
        ):
            self.assertEqual(
                ghostfolio_sync.file_content(snapshot, filename),
                contents[environment[setting]],
            )
        content_urls = [url for url, _kwargs in calls if "/contents/" in url]
        self.assertEqual(len(content_urls), 5)
        self.assertTrue(
            all(url.endswith(f"ref={commit_sha}") for url in content_urls)
        )

    def test_repository_snapshot_rejects_a_non_private_destination(self):
        with (
            patch.dict(ghostfolio_sync.os.environ, repository_environment()),
            patch.object(
                ghostfolio_sync,
                "request_json",
                return_value=(
                    200,
                    {"private": False, "full_name": "owner/canonical-ledger"},
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "not verified private"),
        ):
            ghostfolio_sync.repository_snapshot()

    def test_repository_configuration_rejects_duplicate_artifact_paths(self):
        environment = repository_environment()
        environment[ghostfolio_sync.REPOSITORY_HOLDINGS_PATH_ENV] = environment[
            ghostfolio_sync.REPOSITORY_EVENT_PATH_ENV
        ]
        with (
            patch.dict(ghostfolio_sync.os.environ, environment),
            self.assertRaisesRegex(RuntimeError, "paths must be distinct"),
        ):
            ghostfolio_sync.repository_configuration()

    def test_receipt_append_retries_sha_conflict_and_verifies_durability(self):
        environment = repository_environment()
        other = {"order_id": "OTHER", "event_hash": "c" * 64}
        wanted = {"order_id": "ORDER-1", "event_hash": "d" * 64}
        other_content = ghostfolio_sync.canonical(other) + "\n"
        wanted_content = other_content + ghostfolio_sync.canonical(wanted) + "\n"
        repository_files = [
            {"content": "", "sha": "1" * 40, "exists": True},
            {"content": other_content, "sha": "2" * 40, "exists": True},
            {"content": wanted_content, "sha": "3" * 40, "exists": True},
        ]
        with (
            patch.dict(ghostfolio_sync.os.environ, environment),
            patch.object(ghostfolio_sync, "_verify_private_repository"),
            patch.object(
                ghostfolio_sync,
                "_repository_file",
                side_effect=repository_files,
            ),
            patch.object(
                ghostfolio_sync,
                "_repository_json",
                side_effect=[(409, {}), (200, {})],
            ) as write,
        ):
            self.assertTrue(
                ghostfolio_sync._append_repository_receipt(
                    ghostfolio_sync.RECEIPT_FILE, "order_id", wanted
                )
            )

        self.assertEqual(write.call_count, 2)
        retry_payload = write.call_args_list[1].kwargs["payload"]
        self.assertEqual(retry_payload["sha"], "2" * 40)
        self.assertEqual(retry_payload["branch"], "main")
        self.assertEqual(
            base64.b64decode(retry_payload["content"]).decode("utf-8"),
            wanted_content,
        )
        self.assertNotIn(
            "private-token", write.call_args_list[1].args[1]
        )

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

        scoped_env = (root / "ghostfolio" / "write-service-env.ps1").read_text(
            encoding="utf-8"
        )
        for key in (
            "DCA_OUTBOX_REPOSITORY_OWNER",
            "DCA_OUTBOX_REPOSITORY_NAME",
            "DCA_OUTBOX_REPOSITORY_BRANCH",
            "DCA_OUTBOX_REPOSITORY_TOKEN",
            "DCA_OUTBOX_EVENT_PATH",
            "DCA_OUTBOX_HOLDINGS_PATH",
            "DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH",
            "DCA_OUTBOX_GHOSTFOLIO_HOLDINGS_RECEIPT_PATH",
            "DCA_OUTBOX_GHOSTFOLIO_PROVENANCE_RECEIPT_PATH",
        ):
            self.assertIn(key, scoped_env)
        self.assertNotIn("'GIST_ID'", scoped_env)
        self.assertNotIn("'GIST_TOKEN'", scoped_env)
        for key in (
            "GHOSTFOLIO_RECOVERY_CRYPTO_ORDER_ID",
            "GHOSTFOLIO_RECOVERY_FUNDING_ORDER_ID",
            "GHOSTFOLIO_RECOVERY_EVENT_HASH",
        ):
            self.assertIn(key, scoped_env)

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
            "ETH_GBP": "kraken",
            "SOL_GBP": "kraken",
            "DOGE_GBP": "kraken",
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
                    "ETH_GBP": "kraken",
                    "SOL_GBP": "kraken",
                    "DOGE_GBP": "kraken",
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
        self.assertEqual(quantities["ETH_GBP"], 0)
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value={"files": {}}),
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
                patch.object(ghostfolio_sync, "repository_snapshot", return_value={"files": {}}),
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
                commit=True, token="token", repository_payload={"files": {}}
            )

    def test_sync_once_passes_one_immutable_repository_view_to_reconciliation(self):
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
                    ghostfolio_sync, "repository_snapshot", return_value=payload
                ) as repository_read,
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

        repository_read.assert_called_once_with()
        accounts.assert_called_once_with("token", require_bitkub=False)
        account_map.assert_called_once_with(
            {"Kraken DCA": "kraken"}, require_bitkub=False
        )
        self.assertIs(reconcile.call_args.kwargs["repository_payload"], payload)

    def test_confirmed_hype_reclassification_is_durable_and_quantity_neutral(self):
        payload = hype_recovery_payload()
        remote = json.loads(ghostfolio_sync.canonical(payload))
        item = confirmed_hype_event()
        events = ghostfolio_sync.parse_events(
            ghostfolio_sync.file_content(payload, ghostfolio_sync.EVENT_FILE)
        )
        receipts = set()
        original = [hype_opening_activity()]
        residual = [hype_opening_activity(residual=True)]
        completed = residual + [hype_event_activity(item)]
        quantities = {
            "BTC_GBP": 0.01,
            "SOL_GBP": 2,
        }
        repository_updates = []

        def update_opening(_identity, amount, _event, snapshot_hash, _token):
            self.assertTrue(
                ghostfolio_sync.PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file()
            )
            self.assertEqual(
                ghostfolio_sync.load_provenance_reclassification_intent()["phase"],
                "PREPARED",
            )
            self.assertEqual(amount, ghostfolio_sync.Decimal("1"))
            self.assertEqual(snapshot_hash, SYNTHETIC_OPENING_HASH)

        def publish_repository(_payload, filename, identity_field, receipt):
            self.assertIn(
                filename,
                {
                    ghostfolio_sync.RECEIPT_FILE,
                    ghostfolio_sync.PROVENANCE_RECLASSIFICATION_RECEIPT_FILE,
                },
            )
            self.assertEqual(
                identity_field,
                "order_id"
                if filename == ghostfolio_sync.RECEIPT_FILE
                else "event_id",
            )
            content = ghostfolio_sync.file_content(remote, filename)
            separator = "" if not content or content.endswith("\n") else "\n"
            remote["files"][filename] = {
                "content": content + separator + ghostfolio_sync.canonical(receipt) + "\n"
            }
            repository_updates.append(filename)
            return True

        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {
                    "GHOSTFOLIO_ACCOUNT_MAP": json.dumps(
                        {"HYPE_USD": "kraken"}
                    ),
                },
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_hype_activities",
                side_effect=[original, original, residual, completed],
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_quantities",
                side_effect=[
                    {**quantities, "HYPE_USD": 1.25},
                    {**quantities, "HYPE_USD": 1.25},
                    {**quantities, "HYPE_USD": 1},
                    {**quantities, "HYPE_USD": 1.25},
                ],
            ),
            patch.object(
                ghostfolio_sync,
                "_preflight_recovery_event",
                side_effect=["missing", "missing", "missing"],
            ),
            patch.object(
                ghostfolio_sync,
                "_put_residual_opening_activity",
                side_effect=update_opening,
            ) as update,
            patch.object(
                ghostfolio_sync,
                "_post_recovery_event",
                return_value="hype-recovery-activity",
            ) as imported,
            patch.object(ghostfolio_sync, "flush_ghostfolio_cache"),
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
                "repository_snapshot",
                side_effect=lambda: json.loads(ghostfolio_sync.canonical(remote)),
            ),
            patch.object(
                ghostfolio_sync,
                "append_named_receipt",
                side_effect=publish_repository,
            ),
        ):
            result = ghostfolio_sync.process_hype_provenance_reclassification(
                payload,
                events,
                receipts,
                "token",
                "kraken",
                now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["clear_intent_after_state"])
        self.assertEqual(receipts, {SYNTHETIC_RECOVERY_EVENT_ID})
        self.assertEqual(
            repository_updates,
            [
                ghostfolio_sync.RECEIPT_FILE,
                ghostfolio_sync.PROVENANCE_RECLASSIFICATION_RECEIPT_FILE,
            ],
        )
        update.assert_called_once()
        imported.assert_called_once_with(item, "token")
        intent = ghostfolio_sync.load_provenance_reclassification_intent()
        self.assertEqual(intent["phase"], "RECEIPTS_PUBLISHED")
        _snapshot, original_quantity, residual_quantity, event_quantity = (
            ghostfolio_sync._intent_economics(intent)
        )
        self.assertEqual(original_quantity, ghostfolio_sync.Decimal("1.25"))
        self.assertEqual(residual_quantity, ghostfolio_sync.Decimal("1"))
        self.assertEqual(event_quantity, ghostfolio_sync.Decimal("0.25"))
        update_payload = ghostfolio_sync._opening_update_payload(
            ghostfolio_sync._stable_hype_activity(original[0], "kraken"),
            ghostfolio_sync.Decimal("1"),
            item,
            SYNTHETIC_OPENING_HASH,
        )
        self.assertEqual(update_payload["type"], "BUY")
        self.assertEqual(update_payload["id"], original[0]["id"])
        self.assertEqual(hype_event_activity(item)["type"], "BUY")
        event_rows = ghostfolio_sync._event_receipt_rows(
            ghostfolio_sync.file_content(remote, ghostfolio_sync.RECEIPT_FILE),
            events,
        )
        provenance_rows = (
            ghostfolio_sync.parse_provenance_reclassification_receipts(
                ghostfolio_sync.file_content(
                    remote,
                    ghostfolio_sync.PROVENANCE_RECLASSIFICATION_RECEIPT_FILE,
                ),
                events,
            )
        )
        self.assertIn(SYNTHETIC_RECOVERY_EVENT_ID, event_rows)
        self.assertIn(SYNTHETIC_RECOVERY_EVENT_ID, provenance_rows)

        ghostfolio_sync.clear_provenance_reclassification_intent()
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"HYPE_USD": "kraken"})},
                clear=True,
            ),
            patch.object(
                ghostfolio_sync,
                "ghostfolio_hype_activities",
                return_value=completed,
            ),
        ):
            completed_without_private_env = (
                ghostfolio_sync.process_hype_provenance_reclassification(
                    remote,
                    events,
                    set(event_rows),
                    "token",
                    "kraken",
                )
            )
        self.assertEqual(completed_without_private_env["status"], "COMPLETE")

    def test_completed_hype_reclassification_ignores_later_event_and_reconciliation(self):
        payload = completed_hype_recovery_payload()
        old_event = confirmed_hype_event()
        later_event = {
            **old_event,
            "event_id": "OTEST2-CRYPTO-ORDER",
            "crypto_order_id": "OTEST2-CRYPTO-ORDER",
            "funding_order_id": "OTEST2-FUNDING-ORDER",
            "occurred_at": "2026-08-08T06:02:03Z",
        }
        resign_event(later_event)
        payload["files"][ghostfolio_sync.EVENT_FILE]["content"] += (
            ghostfolio_sync.canonical(later_event) + "\n"
        )
        later_activity = {
            **hype_event_activity(later_event),
            "id": "hype-later-event-activity",
        }
        later_receipt = {
            "order_id": later_event["event_id"],
            "event_hash": later_event["canonical_hash"],
            "ghostfolio_activity_id": later_activity["id"],
            "imported_at": "2026-08-08T06:03:00Z",
        }
        payload["files"][ghostfolio_sync.RECEIPT_FILE]["content"] += (
            ghostfolio_sync.canonical(later_receipt) + "\n"
        )
        rounding_reconciliation = {
            **hype_opening_activity(),
            "id": "hype-rounding-reconciliation",
            "comment": (
                "Kraken opening-balance reconciliation; "
                f"snapshot={'c' * 64}; target=HYPE_USD"
            ),
            "date": "2026-08-08T06:04:00Z",
            "quantity": 0.00000001,
        }

        result = self._process_completed_hype_reclassification(
            [
                hype_opening_activity(residual=True),
                hype_event_activity(old_event),
                later_activity,
                rounding_reconciliation,
            ],
            payload,
        )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertFalse(result["clear_intent_after_state"])

    def test_completed_hype_reclassification_rejects_duplicate_exact_evidence(self):
        old_event = confirmed_hype_event()
        residual = hype_opening_activity(residual=True)
        recovered = hype_event_activity(old_event)
        cases = {
            "residual": [
                residual,
                {**residual, "id": "duplicate-hype-residual"},
                recovered,
            ],
            "recovered": [
                residual,
                recovered,
                {**recovered, "id": "duplicate-hype-recovered"},
            ],
        }
        for label, activities in cases.items():
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(RuntimeError, "topology is ambiguous"),
            ):
                self._process_completed_hype_reclassification(activities)

    def test_completed_hype_reclassification_rejects_unrelated_activity_at_or_before_completion(self):
        old_event = confirmed_hype_event()
        unrelated = {
            **hype_event_activity({
                **old_event,
                "event_id": "OTEST2-CRYPTO-ORDER",
                "crypto_order_id": "OTEST2-CRYPTO-ORDER",
                "funding_order_id": "OTEST2-FUNDING-ORDER",
            }),
            "id": "unrelated-hype-activity",
        }
        for activity_date in (
            "2026-08-07T08:29:59Z",
            SYNTHETIC_RECOVERY_COMPLETED_AT,
        ):
            with (
                self.subTest(activity_date=activity_date),
                self.assertRaisesRegex(RuntimeError, "topology is ambiguous"),
            ):
                self._process_completed_hype_reclassification([
                    hype_opening_activity(residual=True),
                    hype_event_activity(old_event),
                    {**unrelated, "date": activity_date},
                ])

    def test_completed_hype_reclassification_rejects_missing_or_malformed_activity_dates(self):
        old_event = confirmed_hype_event()
        unrelated = {
            **hype_event_activity({
                **old_event,
                "event_id": "OTEST2-CRYPTO-ORDER",
                "crypto_order_id": "OTEST2-CRYPTO-ORDER",
                "funding_order_id": "OTEST2-FUNDING-ORDER",
            }),
            "id": "unrelated-hype-activity",
        }
        cases = {
            "missing": {
                key: value for key, value in unrelated.items() if key != "date"
            },
            "malformed": {**unrelated, "date": "not-a-timestamp"},
        }
        for label, activity in cases.items():
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(RuntimeError, "has no valid ISO timestamp"),
            ):
                self._process_completed_hype_reclassification([
                    hype_opening_activity(residual=True),
                    hype_event_activity(old_event),
                    activity,
                ])

    def test_residual_opening_update_uses_exact_put_contract(self):
        item = confirmed_hype_event()
        identity = ghostfolio_sync._stable_hype_activity(
            hype_opening_activity(), "kraken"
        )
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_URL": "http://ghostfolio.test"},
            ),
            patch.object(
                ghostfolio_sync,
                "request_json",
                return_value=(200, {}),
            ) as request,
        ):
            ghostfolio_sync._put_residual_opening_activity(
                identity,
                ghostfolio_sync.Decimal("1"),
                item,
                SYNTHETIC_OPENING_HASH,
                "security-token",
            )

        request.assert_called_once_with(
            "http://ghostfolio.test/api/v1/activities/hype-opening-activity",
            method="PUT",
            token="security-token",
            payload={
                "accountId": "kraken",
                "comment": (
                    "Kraken opening-balance residual after PortfolioEvent recovery; "
                    f"snapshot={SYNTHETIC_OPENING_HASH}; target=HYPE_USD; "
                    f"event={SYNTHETIC_RECOVERY_EVENT_ID}"
                ),
                "currency": "USD",
                "dataSource": "YAHOO",
                "date": "2026-08-07T06:30:00.000000Z",
                "fee": 0.0,
                "id": "hype-opening-activity",
                "quantity": 1.0,
                "symbol": "HYPE32196USD",
                "tags": [],
                "type": "BUY",
                "unitPrice": 40.0,
            },
        )

    def test_sync_once_keeps_provenance_intent_and_refuses_mutating_drift(self):
        payload = {"files": {}}
        holdings = {
            "status": "DRIFT",
            "drift": {"SOL_GBP": ghostfolio_sync.Decimal("0.1")},
            "snapshot_as_of": "2026-08-07T04:00:00Z",
            "snapshot_hash": "a" * 64,
            "portfolio_calculation_status": "OK",
            "portfolio_calculation_has_error": False,
        }
        provenance = {
            "status": "COMPLETE",
            "event_id": SYNTHETIC_RECOVERY_EVENT_ID,
            "event_hash": confirmed_hype_event()["canonical_hash"],
            "opening_snapshot_hash": SYNTHETIC_OPENING_HASH,
            "receipt_present": True,
            "clear_intent_after_state": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with (
                patch.object(ghostfolio_sync, "STATE_PATH", state_path),
                patch.object(ghostfolio_sync, "repository_snapshot", return_value=payload),
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
                    "process_hype_provenance_reclassification",
                    return_value=provenance,
                ),
                patch.object(
                    ghostfolio_sync,
                    "reconcile_holdings_snapshot",
                    return_value=holdings,
                ) as reconcile,
                patch.object(
                    ghostfolio_sync,
                    "clear_provenance_reclassification_intent",
                ) as clear_intent,
                self.assertRaisesRegex(RuntimeError, "holdings drift blocks"),
            ):
                ghostfolio_sync.sync_once()

        reconcile.assert_called_once_with(
            commit=False,
            token="token",
            repository_payload=payload,
            allow_provenance_intent=True,
        )
        clear_intent.assert_not_called()
        self.assertFalse(state_path.exists())

    def test_unreceipted_hype_event_requires_private_recovery_evidence(self):
        payload = hype_recovery_payload()
        events = ghostfolio_sync.parse_events(
            ghostfolio_sync.file_content(payload, ghostfolio_sync.EVENT_FILE)
        )
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"HYPE_USD": "kraken"})},
                clear=True,
            ),
            patch.object(ghostfolio_sync, "ghostfolio_hype_activities") as read,
            self.assertRaisesRegex(RuntimeError, "recovery evidence is required"),
        ):
            ghostfolio_sync.process_hype_provenance_reclassification(
                payload, events, set(), "token", "kraken"
            )
        read.assert_not_called()

    def test_hype_recovery_fails_closed_on_ambiguity_negative_residual_and_drift(self):
        item = confirmed_hype_event()
        opening = hype_opening_activity()
        duplicate = dict(opening, id="second-opening")
        with (
            patch.dict(
                ghostfolio_sync.os.environ,
                {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"HYPE_USD": "kraken"})},
            ),
            self.assertRaisesRegex(RuntimeError, "topology is ambiguous"),
        ):
            ghostfolio_sync._hype_activity_topology(
                [opening, duplicate], "kraken", item
            )

        payload = hype_recovery_payload()
        payload["files"][ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE]["content"] = (
            ghostfolio_sync.canonical(
                resign_snapshot({
                    **synthetic_recovery_snapshot(),
                    "holdings": {
                        **synthetic_recovery_snapshot()["holdings"],
                        "HYPE_USD": {
                            **synthetic_recovery_snapshot()["holdings"]["HYPE_USD"],
                            "quantity": "0.2",
                        },
                    },
                })
            )
        )
        payload["files"][ghostfolio_sync.HOLDINGS_RECEIPT_FILE]["content"] = (
            ghostfolio_sync.canonical({
                "snapshot_hash": SYNTHETIC_OPENING_HASH,
                "reconciled_at": "2026-08-07T06:35:00Z",
                "adjustments": [
                    {"target": "HYPE_USD", "quantity_delta": "0.2"}
                ],
            })
            + "\n"
        )
        small_opening = hype_opening_activity()
        small_opening["quantity"] = 0.2
        events = [item]
        with self.assertRaisesRegex(RuntimeError, "residual is not positive"):
            ghostfolio_sync._recovery_snapshot(
                payload,
                events,
                item,
                ghostfolio_sync._stable_hype_activity(small_opening, "kraken"),
                now=datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc),
            )

        with self.assertRaisesRegex(RuntimeError, "drift.*HYPE_USD"):
            ghostfolio_sync._require_recovery_quantities(
                synthetic_recovery_snapshot(),
                {"BTC_GBP": 0.01, "HYPE_USD": 1.2, "SOL_GBP": 2},
                ghostfolio_sync.Decimal("1.25"),
            )

    def test_hype_reclassification_recovers_after_each_remote_crash_boundary(self):
        item = confirmed_hype_event()
        now = datetime(2026, 8, 7, 8, 30, tzinfo=timezone.utc)
        balances = {"BTC_GBP": 0.01, "SOL_GBP": 2}

        for crash_point in ("after_put", "after_import", "after_receipts"):
            with self.subTest(crash_point=crash_point):
                ghostfolio_sync.clear_provenance_reclassification_intent()
                remote = hype_recovery_payload()
                state = {
                    "opening_reduced": False,
                    "event_imported": False,
                    "crashed": False,
                    "put_count": 0,
                    "import_count": 0,
                    "publish_count": 0,
                }

                def activities(_token, _account):
                    result = [
                        hype_opening_activity(residual=state["opening_reduced"])
                    ]
                    if state["event_imported"]:
                        result.append(hype_event_activity(item))
                    return result

                def quantities(_token, _account):
                    hype = 1 if state["opening_reduced"] else 1.25
                    if state["event_imported"]:
                        hype += 0.25
                    return {**balances, "HYPE_USD": hype}

                def preflight(_event, _token):
                    return "duplicate" if state["event_imported"] else "missing"

                def put_opening(
                    _identity, _residual, _event, _snapshot_hash, _token
                ):
                    state["opening_reduced"] = True
                    state["put_count"] += 1
                    if crash_point == "after_put" and not state["crashed"]:
                        state["crashed"] = True
                        raise RuntimeError("simulated crash after PUT")

                def import_event(_event, _token):
                    state["event_imported"] = True
                    state["import_count"] += 1
                    if crash_point == "after_import" and not state["crashed"]:
                        state["crashed"] = True
                        raise RuntimeError("simulated crash after import")
                    return "hype-recovery-activity"

                def publish(intent):
                    state["publish_count"] += 1
                    event_receipt, provenance_receipt = (
                        ghostfolio_sync._migration_receipts(
                            intent["event"], intent
                        )
                    )
                    files = remote.setdefault("files", {})
                    files[ghostfolio_sync.RECEIPT_FILE] = {
                        "content": ghostfolio_sync.canonical(event_receipt)
                        + "\n"
                    }
                    files[
                        ghostfolio_sync.PROVENANCE_RECLASSIFICATION_RECEIPT_FILE
                    ] = {
                        "content": ghostfolio_sync.canonical(provenance_receipt)
                        + "\n"
                    }
                    if crash_point == "after_receipts" and not state["crashed"]:
                        state["crashed"] = True
                        raise RuntimeError("simulated crash after receipts")

                with (
                    patch.dict(
                        ghostfolio_sync.os.environ,
                        {
                            "GHOSTFOLIO_ACCOUNT_MAP": json.dumps(
                                {"HYPE_USD": "kraken"}
                            )
                        },
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "ghostfolio_hype_activities",
                        side_effect=activities,
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "ghostfolio_quantities",
                        side_effect=quantities,
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "_preflight_recovery_event",
                        side_effect=preflight,
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "_put_residual_opening_activity",
                        side_effect=put_opening,
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "_post_recovery_event",
                        side_effect=import_event,
                    ),
                    patch.object(
                        ghostfolio_sync,
                        "_publish_recovery_receipts",
                        side_effect=publish,
                    ),
                    patch.object(ghostfolio_sync, "flush_ghostfolio_cache"),
                    patch.object(
                        ghostfolio_sync,
                        "ghostfolio_portfolio_calculation",
                        return_value={
                            "portfolio_calculation_status": "OK",
                            "portfolio_calculation_has_error": False,
                        },
                    ),
                ):
                    first_payload = json.loads(ghostfolio_sync.canonical(remote))
                    first_events = ghostfolio_sync.parse_events(
                        ghostfolio_sync.file_content(
                            first_payload, ghostfolio_sync.EVENT_FILE
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                        ghostfolio_sync.process_hype_provenance_reclassification(
                            first_payload,
                            first_events,
                            set(),
                            "token",
                            "kraken",
                            now=now,
                        )
                    self.assertTrue(
                        ghostfolio_sync.PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file()
                    )

                    resume_payload = json.loads(ghostfolio_sync.canonical(remote))
                    resume_events = ghostfolio_sync.parse_events(
                        ghostfolio_sync.file_content(
                            resume_payload, ghostfolio_sync.EVENT_FILE
                        )
                    )
                    resume_receipts = ghostfolio_sync.parse_event_receipts(
                        ghostfolio_sync.file_content(
                            resume_payload, ghostfolio_sync.RECEIPT_FILE
                        ),
                        resume_events,
                    )
                    result = ghostfolio_sync.process_hype_provenance_reclassification(
                        resume_payload,
                        resume_events,
                        resume_receipts,
                        "token",
                        "kraken",
                        now=now,
                    )

                self.assertEqual(result["status"], "COMPLETE")
                self.assertEqual(state["put_count"], 1)
                self.assertEqual(state["import_count"], 1)
                self.assertAlmostEqual(quantities(None, None)["HYPE_USD"], 1.25)
                self.assertGreaterEqual(state["publish_count"], 1)

    def test_hype_recovery_intent_rejects_a_newer_outbox_event(self):
        item = confirmed_hype_event()
        snapshot = synthetic_recovery_snapshot()
        opening = ghostfolio_sync._stable_hype_activity(
            hype_opening_activity(), "kraken"
        )
        ghostfolio_sync.save_provenance_reclassification_intent({
            "version": 1,
            "phase": "PREPARED",
            "event": item,
            "snapshot": snapshot,
            "baseline_event_ids": [item["event_id"]],
            "opening_snapshot_hash": SYNTHETIC_OPENING_HASH,
            "opening_original": opening,
            "event_activity_id": None,
            "completed_at": None,
        })
        newer = event("NEWER-SOL-EVENT")
        newer["occurred_at"] = "2026-08-07T08:05:00Z"
        resign_event(newer)
        payload = hype_recovery_payload()
        payload["files"][ghostfolio_sync.EVENT_FILE]["content"] += (
            ghostfolio_sync.canonical(newer) + "\n"
        )
        events = ghostfolio_sync.parse_events(
            payload["files"][ghostfolio_sync.EVENT_FILE]["content"]
        )
        with (
            patch.object(ghostfolio_sync, "ghostfolio_hype_activities") as read,
            self.assertRaisesRegex(RuntimeError, "newer or changed"),
        ):
            ghostfolio_sync.process_hype_provenance_reclassification(
                payload, events, set(), "token", "kraken"
            )
        read.assert_not_called()

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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=payload),
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
        reference = datetime.now(timezone.utc).replace(microsecond=0)
        old_snapshot = signed_snapshot()
        old_snapshot["as_of"] = (
            reference - timedelta(minutes=60)
        ).isoformat().replace("+00:00", "Z")
        resign_snapshot(old_snapshot)
        receipt_value = {
            "snapshot_hash": old_snapshot["canonical_hash"],
            "reconciled_at": (
                reference - timedelta(minutes=45)
            ).isoformat().replace("+00:00", "Z"),
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
        new_event["occurred_at"] = (
            reference - timedelta(minutes=30)
        ).isoformat().replace("+00:00", "Z")
        resign_event(new_event)
        current_snapshot = json.loads(
            ghostfolio_sync.canonical(old_snapshot)
        )
        current_snapshot["as_of"] = (
            reference - timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z")
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
                    },
                ),
                patch.object(
                    ghostfolio_sync, "STATE_PATH", Path(directory) / "state.json"
                ),
                patch.object(ghostfolio_sync, "repository_snapshot", return_value=payload),
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
        repository_payload = {
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
        repository_payload = {
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
        repository_payload = {
            "files": {
                ghostfolio_sync.HOLDINGS_SNAPSHOT_FILE: {
                    "content": ghostfolio_sync.canonical(snapshot)
                }
            }
        }
        with (
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=payload),
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
                repository_payload=payload,
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=payload),
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
                repository_payload=payload,
            )

        self.assertEqual(result["status"], "RECONCILED")
        flush.assert_called_once_with("token")
        append.assert_called_once()
        self.assertFalse(ghostfolio_sync.HOLDINGS_INTENT_PATH.exists())

    def test_calculation_error_retains_intent_and_blocks_receipt(self):
        snapshot = signed_snapshot()
        repository_payload = {
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
        repository_payload = {
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
        repository_payload = {
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
            patch.object(ghostfolio_sync, "repository_snapshot", return_value=repository_payload),
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
                    "provenance_reclassification_status": "NOT_REQUIRED",
                    "provenance_reclassification_event_id": None,
                    "provenance_reclassification_event_hash": None,
                    "provenance_reclassification_opening_snapshot_hash": None,
                    "provenance_reclassification_receipt_present": False,
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

                state_path.write_text(
                    json.dumps({
                        "holdings_status": "IN_SYNC",
                        "portfolio_calculation_has_error": False,
                        **healthy_fields,
                        "provenance_reclassification_status": "COMPLETE",
                        "provenance_reclassification_event_id": (
                            SYNTHETIC_RECOVERY_EVENT_ID
                        ),
                        "provenance_reclassification_event_hash": (
                            confirmed_hype_event()["canonical_hash"]
                        ),
                        "provenance_reclassification_opening_snapshot_hash": (
                            SYNTHETIC_OPENING_HASH
                        ),
                        "provenance_reclassification_receipt_present": True,
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(ghostfolio_sync.main(), 0)
                ghostfolio_sync.PROVENANCE_RECLASSIFICATION_INTENT_PATH.write_text(
                    "{}", encoding="utf-8"
                )
                self.assertEqual(ghostfolio_sync.main(), 1)
                ghostfolio_sync.PROVENANCE_RECLASSIFICATION_INTENT_PATH.unlink()

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
                parsed,
                {
                    "BTC_GBP": 0.1,
                    "HYPE_USD": 2,
                    "ETH_GBP": 99,
                    "SOL_GBP": 3,
                },
            ),
            {"BTC_GBP": 0.1},
        )
        current = ghostfolio_sync.parse_holdings_snapshot(
            ghostfolio_sync.canonical(current_signed_snapshot())
        )
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["holdings"]["ETH_GBP"]["pair"], "ETH/GBP")
        self.assertEqual(
            ghostfolio_sync.holdings_drift(
                current,
                {
                    "BTC_GBP": 0.2,
                    "HYPE_USD": 2,
                    "ETH_GBP": 0.3,
                    "SOL_GBP": 3,
                },
            ),
            {"ETH_GBP": 0.10000000000000003},
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
            "unsupported_nonzero_assets": ["XRP"],
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

    def test_eth_uses_direct_gbp_event_contract_and_coingecko_profile(self):
        item = event("ORDER-ETH")
        item.update({
            "target": "ETH_GBP",
            "base_currency": "ETH",
            "crypto_quantity": "0.004",
            "unit_price_quote": "2500",
        })
        item = resign_event(item)
        self.assertEqual(
            ghostfolio_sync.parse_events(ghostfolio_sync.canonical(item)),
            [item],
        )
        with patch.dict(
            ghostfolio_sync.os.environ,
            {"GHOSTFOLIO_ACCOUNT_MAP": json.dumps({"ETH_GBP": "local-eth"})},
        ):
            activity = ghostfolio_sync.import_payload(item)["activities"][0]
        self.assertEqual(activity["accountId"], "local-eth")
        self.assertEqual(activity["dataSource"], "COINGECKO")
        self.assertEqual(activity["symbol"], "ethereum")


if __name__ == "__main__":
    unittest.main()

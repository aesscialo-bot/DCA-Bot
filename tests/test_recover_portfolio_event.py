import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import gist_logger
from github_contents import RepositoryFile, WriteResult
import kraken_client
import recover_portfolio_event as recovery


# Public, explicitly sanitized placeholders with Kraken's order-ID shape.  The
# production IDs are operator-supplied secrets and must never enter this repo.
CRYPTO_ORDER_ID = "OSANIT-IZED0-CRYPTO"
FUNDING_ORDER_ID = "OSANIT-IZED0-FUNDNG"
FILL_TIMESTAMP = 1_786_080_840  # Synthetic: 2026-08-07 12:34 Asia/Bangkok
EXPECTED_SYNTHETIC_ROW = (
    "| 2026-08-07 12:34 +07 | GBP 12.50 | 1.250000 | USD 15.6000 | "
    "USD 15.5000 | 0.02500000 USD (GBP equiv 0.02), 0.00125000 HYPE "
    "(GBP equiv 0.05) | USD 50.0000 | 0.30875000 HYPE | "
    f"{FUNDING_ORDER_ID} | {CRYPTO_ORDER_ID} | optional/not saved |\n"
)
EXPECTED_SYNTHETIC_ROW_SHA256 = (
    "a0ab58f4a3bde51d0033de62cf5ecf55cdad241088960a0e1c2a99c3342969df"
)
EXPECTED_SYNTHETIC_EVENT_HASH = (
    "505eb48f7c80b1deed5152aa91b2e1500e11b8cc869d3dc16f7068ddd6ebe8d7"
)
EXPECTED_SYNTHETIC_EVENT_SHA256 = (
    "326add73c55a25c1ade986c64015c6ff89e1258cc73179fa8142f3c60195d073"
)


def request(**changes):
    values = {
        "target": recovery.INCIDENT_TARGET,
        "trade_date": recovery.INCIDENT_TRADE_DATE,
        "budget_gbp": recovery.INCIDENT_BUDGET_GBP,
        "expected_crypto_order_id": CRYPTO_ORDER_ID,
        "expected_funding_order_id": FUNDING_ORDER_ID,
    }
    values.update(changes)
    return recovery.RecoveryRequest(**values)


def reconciled_fill(**changes):
    buy_client_id = kraken_client.build_client_order_id(
        recovery.INCIDENT_TARGET,
        recovery.INCIDENT_TRADE_DATE,
        purpose="buy",
    )
    funding_client_id = kraken_client.build_client_order_id(
        recovery.INCIDENT_TARGET,
        recovery.INCIDENT_TRADE_DATE,
        purpose="funding",
    )
    values = {
        "order_id": CRYPTO_ORDER_ID,
        "client_order_id": buy_client_id,
        "funding_order_id": FUNDING_ORDER_ID,
        "funding_client_order_id": funding_client_id,
        "pair": "HYPE/USD",
        "quote_currency": "USD",
        "cost_gbp": 15.5 / 1.25,
        "fee_gbp": (0.025 + 0.0625) / 1.25,
        "gbp_fee_debit": 0.0,
        "spent_gbp": 12.5,
        "received": 0.30875000,
        "market_gbp_price_per_unit": 50.0 / 1.25,
        "effective_gbp_price_per_unit": 12.5 / 0.30875000,
        "funded_usd": 15.6000,
        "funding_fee_usd": 0.02500000,
        "crypto_fee_usd": 0.06250000,
        "cost_usd": 15.5000,
        "usd_fee_debit": 0.0,
        "market_usd_price_per_unit": 50.0000,
        "effective_usd_price_per_unit": 15.5 / 0.30875000,
        "gbp_usd_rate": 1.250000,
        "fee_details": [
            {
                "leg": "funding",
                "currency": "USD",
                "amount": 0.02500000,
                "quote_equivalent": 0.02500000,
                "usd_equivalent": 0.02500000,
                "gbp_equivalent": 0.02500000 / 1.250000,
            },
            {
                "leg": "buy",
                "currency": "HYPE",
                "amount": 0.00125000,
                "quote_equivalent": 0.06250000,
                "usd_equivalent": 0.06250000,
                "gbp_equivalent": 0.06250000 / 1.250000,
            },
        ],
        "timestamp": FILL_TIMESTAMP,
    }
    values.update(changes)
    return values


def closed_funding_order(funding_client_id):
    return {
        "id": FUNDING_ORDER_ID,
        "clientOrderId": funding_client_id,
        "symbol": "GBP/USD",
        "side": "sell",
        "status": "closed",
        "filled": 12.5,
        "cost": 15.625,
        "fee": {"cost": 0.025, "currency": "USD"},
        "info": {"fee": "0.025", "oflags": "fciq"},
        "lastUpdateTimestamp": FILL_TIMESTAMP * 1000,
    }


def closed_crypto_order(buy_client_id):
    return {
        "id": CRYPTO_ORDER_ID,
        "clientOrderId": buy_client_id,
        "symbol": "HYPE/USD",
        "side": "buy",
        "status": "closed",
        "filled": 0.31000000,
        "cost": 15.5000,
        "fee": {"cost": 0.00125000, "currency": "HYPE"},
        "info": {"fee": "0.06250000", "oflags": "fcib"},
        "lastUpdateTimestamp": FILL_TIMESTAMP * 1000,
    }


def synthetic_delivery():
    buy_client_id = kraken_client.build_client_order_id(
        recovery.INCIDENT_TARGET,
        recovery.INCIDENT_TRADE_DATE,
        purpose="buy",
    )
    funding_client_id = kraken_client.build_client_order_id(
        recovery.INCIDENT_TARGET,
        recovery.INCIDENT_TRADE_DATE,
        purpose="funding",
    )
    trade_data = recovery._authoritative_trade_data(
        reconciled_fill(),
        request(),
        client_order_id=buy_client_id,
        funding_client_order_id=funding_client_id,
    )
    return recovery.build_gist_delivery(
        trade_data, "HYPE", saved_to_ghostfolio=False
    )


def event_content(delivery):
    return json.dumps(
        delivery["event"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"


def outbox_environment():
    return {
        "DCA_OUTBOX_REPOSITORY_OWNER": "example",
        "DCA_OUTBOX_REPOSITORY_NAME": "private-outbox",
        "DCA_OUTBOX_REPOSITORY_BRANCH": "main",
        "DCA_OUTBOX_REPOSITORY_TOKEN": "sanitized-token",
        "DCA_OUTBOX_AUDIT_PATH": "portfolio/audit.md",
        "DCA_OUTBOX_EVENT_PATH": "portfolio/events.jsonl",
        "DCA_OUTBOX_HOLDINGS_PATH": "portfolio/holdings.json",
    }


class FakeRepository:
    def __init__(self, markdown="", events=""):
        self.contents = {
            "portfolio/audit.md": markdown,
            "portfolio/events.jsonl": events,
        }
        self.update_calls = []

    def read_text(self, path):
        content = self.contents.get(path, "")
        return RepositoryFile(content=content, sha="a" * 40, exists=path in self.contents)

    def update_text(self, path, transform, *, message):
        self.update_calls.append((path, message))
        current = self.contents.get(path, "")
        updated = transform(current)
        self.contents[path] = updated
        return WriteResult(changed=updated != current, sha="b" * 40, content=updated)


class PortfolioEventRecoveryTests(unittest.TestCase):
    def test_fully_synthetic_row_and_event_hashes_are_stable(self):
        delivery = synthetic_delivery()

        self.assertEqual(delivery["row"], EXPECTED_SYNTHETIC_ROW)
        self.assertEqual(
            delivery["row_sha256"], EXPECTED_SYNTHETIC_ROW_SHA256
        )
        self.assertEqual(
            delivery["event"]["canonical_hash"],
            EXPECTED_SYNTHETIC_EVENT_HASH,
        )
        self.assertEqual(
            delivery["event_sha256"], EXPECTED_SYNTHETIC_EVENT_SHA256
        )
        self.assertEqual(
            delivery["event"],
            {
                "event_version": 3,
                "event_id": CRYPTO_ORDER_ID,
                "occurred_at": "2026-08-07T05:34:00Z",
                "target": "HYPE_USD",
                "base_currency": "HYPE",
                "quote_currency": "USD",
                "budget_currency": "GBP",
                "funding_order_id": FUNDING_ORDER_ID,
                "crypto_order_id": CRYPTO_ORDER_ID,
                "gbp_debit": "12.5",
                "gbp_usd_rate": "1.25",
                "funded_usd": "15.6",
                "route": "GBP_TO_USD",
                "crypto_cost_quote": "15.5",
                "crypto_quantity": "0.30875",
                "unit_price_quote": "50",
                "funding_fee_quote": "0.025",
                "crypto_fee_quote": "0.0625",
                "canonical_hash": EXPECTED_SYNTHETIC_EVENT_HASH,
            },
        )

    def test_preview_uses_only_reconciliation_and_never_publishes(self):
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ) as reconcile,
            patch.object(
                recovery,
                "_inspect_and_maybe_publish_event",
                return_value="event_missing",
            ) as inspect,
        ):
            summary = recovery.recover_portfolio_event(request())

        buy_client_id = kraken_client.build_client_order_id(
            "HYPE_USD", date(2026, 8, 7), purpose="buy"
        )
        funding_client_id = kraken_client.build_client_order_id(
            "HYPE_USD", date(2026, 8, 7), purpose="funding"
        )
        reconcile.assert_called_once_with(
            "HYPE_USD",
            12.5,
            client_order_id=buy_client_id,
            funding_client_order_id=funding_client_id,
            reconcile_only=True,
        )
        inspect.assert_called_once()
        self.assertFalse(inspect.call_args.kwargs["publish"])
        self.assertEqual(summary["mode"], "preview")
        self.assertEqual(summary["outbox_status"], "event_missing")
        self.assertEqual(summary["timestamp"], "2026-08-07T05:34:00Z")
        self.assertRegex(summary["event_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(summary["row_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(summary),
            {
                "mode",
                "target",
                "trade_date",
                "timestamp",
                "crypto_order_id",
                "funding_order_id",
                "client_order_id",
                "funding_client_order_id",
                "event_hash",
                "row_sha256",
                "outbox_status",
            },
        )

    def test_real_connector_reconcile_path_cannot_submit_either_leg(self):
        buy_client_id = kraken_client.build_client_order_id(
            "HYPE_USD", recovery.INCIDENT_TRADE_DATE, purpose="buy"
        )
        funding_client_id = kraken_client.build_client_order_id(
            "HYPE_USD", recovery.INCIDENT_TRADE_DATE, purpose="funding"
        )
        exchange = MagicMock()
        exchange.markets = {"GBP/USD": {}, "HYPE/USD": {}}
        exchange.fetch_open_orders.return_value = []
        exchange.fetch_closed_orders.side_effect = lambda symbol, params: (
            [closed_funding_order(funding_client_id)]
            if symbol == "GBP/USD"
            else [closed_crypto_order(buy_client_id)]
        )

        with (
            patch.object(kraken_client, "get_kraken_exchange", return_value=exchange),
            patch.object(
                recovery,
                "_inspect_and_maybe_publish_event",
                return_value="event_missing",
            ) as inspect,
        ):
            summary = recovery.recover_portfolio_event(request())

        self.assertEqual(summary["target"], "HYPE_USD")
        inspect.assert_called_once()
        exchange.fetch_balance.assert_not_called()
        exchange.create_market_sell_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_publish_requires_both_reviewed_hashes_before_kraken_access(self):
        with (
            patch.object(recovery, "place_gbp_funded_market_buy") as reconcile,
            patch.object(recovery, "_inspect_and_maybe_publish_event") as inspect,
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused,
                "requires both reviewed event and Markdown-row hashes",
            ):
                recovery.recover_portfolio_event(request(), publish=True)

        reconcile.assert_not_called()
        inspect.assert_not_called()

    def test_wrong_reviewed_hash_never_reaches_gist(self):
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ),
            patch.object(recovery, "_inspect_and_maybe_publish_event") as inspect,
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused,
                "reviewed event hash does not match",
            ):
                recovery.recover_portfolio_event(
                    request(),
                    publish=True,
                    expected_event_hash="0" * 64,
                    expected_row_sha256=synthetic_delivery()["row_sha256"],
                )

        inspect.assert_not_called()

    def test_wrong_reviewed_row_hash_never_reaches_gist(self):
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ),
            patch.object(
                recovery, "_inspect_and_maybe_publish_event"
            ) as inspect,
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused,
                "reviewed Markdown-row hash does not match",
            ):
                recovery.recover_portfolio_event(
                    request(),
                    publish=True,
                    expected_event_hash=EXPECTED_SYNTHETIC_EVENT_HASH,
                    expected_row_sha256="0" * 64,
                )

        inspect.assert_not_called()

    def test_reviewed_publish_delivers_the_exact_preview_once(self):
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ) as reconcile,
            patch.object(
                recovery,
                "_inspect_and_maybe_publish_event",
                side_effect=["event_missing", "event_appended"],
            ) as inspect,
        ):
            preview = recovery.recover_portfolio_event(request())
            published = recovery.recover_portfolio_event(
                request(),
                publish=True,
                expected_event_hash=preview["event_hash"],
                expected_row_sha256=preview["row_sha256"],
            )

        self.assertEqual(reconcile.call_count, 2)
        self.assertTrue(
            all(call.kwargs["reconcile_only"] for call in reconcile.call_args_list)
        )
        self.assertEqual(inspect.call_count, 2)
        delivered = inspect.call_args.args[0]
        self.assertEqual(
            delivered["event"]["canonical_hash"], preview["event_hash"]
        )
        self.assertTrue(inspect.call_args.kwargs["publish"])
        self.assertEqual(published["outbox_status"], "event_appended")

    def test_repeated_publish_reuses_identical_delivery_and_client_ids(self):
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ) as reconcile,
            patch.object(
                recovery,
                "_inspect_and_maybe_publish_event",
                side_effect=[
                    "event_missing",
                    "event_appended",
                    "exact_event_present",
                ],
            ) as inspect,
        ):
            preview = recovery.recover_portfolio_event(request())
            first = recovery.recover_portfolio_event(
                request(),
                publish=True,
                expected_event_hash=preview["event_hash"],
                expected_row_sha256=preview["row_sha256"],
            )
            second = recovery.recover_portfolio_event(
                request(),
                publish=True,
                expected_event_hash=preview["event_hash"],
                expected_row_sha256=preview["row_sha256"],
            )

        self.assertEqual(first["event_hash"], second["event_hash"])
        self.assertEqual(first["row_sha256"], second["row_sha256"])
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(
            inspect.call_args_list[1].args[0], inspect.call_args_list[2].args[0]
        )
        self.assertEqual(
            reconcile.call_args_list[0].kwargs["client_order_id"],
            reconcile.call_args_list[2].kwargs["client_order_id"],
        )
        self.assertEqual(
            reconcile.call_args_list[0].kwargs["funding_client_order_id"],
            reconcile.call_args_list[2].kwargs["funding_client_order_id"],
        )

    def test_incident_coordinates_fail_before_reconciliation(self):
        invalid_requests = (
            request(target="BTC_GBP"),
            request(trade_date=date(2026, 8, 6)),
            request(budget_gbp=Decimal("12.51")),
            request(expected_crypto_order_id=FUNDING_ORDER_ID),
            request(expected_crypto_order_id="O.INVALID"),
            request(expected_funding_order_id="O:INVALID"),
        )
        for invalid in invalid_requests:
            with self.subTest(invalid=invalid):
                with patch.object(
                    recovery, "place_gbp_funded_market_buy"
                ) as reconcile:
                    with self.assertRaises(recovery.RecoveryRefused):
                        recovery.recover_portfolio_event(invalid)
                    reconcile.assert_not_called()

    def test_reconciled_evidence_mismatches_fail_closed(self):
        mismatches = (
            {"order_id": "O-WRONG"},
            {"funding_order_id": "O-WRONG"},
            {"client_order_id": "dca-00000000000000"},
            {"funding_client_order_id": "dca-00000000000000"},
            {"pair": "HYPE/GBP"},
            {"quote_currency": "GBP"},
            {"spent_gbp": 12.49},
            {"timestamp": FILL_TIMESTAMP - 86_400},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with (
                    patch.object(
                        recovery,
                        "place_gbp_funded_market_buy",
                        return_value=reconciled_fill(**mismatch),
                    ),
                    patch.object(
                        recovery, "_inspect_and_maybe_publish_event"
                    ) as inspect,
                ):
                    with self.assertRaises(recovery.RecoveryRefused):
                        recovery.recover_portfolio_event(request())
                    inspect.assert_not_called()

    def test_missing_markdown_row_is_refused_without_patch(self):
        delivery = synthetic_delivery()
        repository = FakeRepository()
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused, "exact Markdown row is missing"
            ):
                recovery._inspect_and_maybe_publish_event(
                    delivery, publish=True
                )

        self.assertEqual(repository.update_calls, [])

    def test_conflicting_markdown_row_is_refused_without_patch(self):
        delivery = synthetic_delivery()
        conflicting_row = delivery["row"].replace("GBP 12.50", "GBP 12.51")
        repository = FakeRepository(conflicting_row, "")
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused, "Markdown row conflicts"
            ):
                recovery._inspect_and_maybe_publish_event(
                    delivery, publish=True
                )

        self.assertEqual(repository.update_calls, [])

    def test_conflicting_event_is_refused_without_patch(self):
        delivery = synthetic_delivery()
        conflicting_event = dict(delivery["event"], funded_usd="16.775")
        conflicting_content = json.dumps(
            conflicting_event,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        repository = FakeRepository(delivery["row"], conflicting_content)
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            with self.assertRaisesRegex(
                recovery.RecoveryRefused, "Portfolio event conflicts"
            ):
                recovery._inspect_and_maybe_publish_event(
                    delivery, publish=True
                )

        self.assertEqual(repository.update_calls, [])

    def test_exact_duplicate_is_idempotent_without_patch(self):
        delivery = synthetic_delivery()
        repository = FakeRepository(delivery["row"], event_content(delivery))
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            status = recovery._inspect_and_maybe_publish_event(
                delivery, publish=True
            )

        self.assertEqual(status, "exact_event_present")
        self.assertEqual(repository.update_calls, [])

    def test_exact_append_updates_only_event_file_and_verifies_content(self):
        delivery = synthetic_delivery()
        appended_events = event_content(delivery)
        repository = FakeRepository(delivery["row"], "")
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            status = recovery._inspect_and_maybe_publish_event(
                delivery, publish=True
            )

        self.assertEqual(status, "event_appended")
        self.assertEqual(repository.update_calls, [
            ("portfolio/events.jsonl", "Recover reviewed PortfolioEventV3")
        ])
        self.assertEqual(repository.contents["portfolio/events.jsonl"], appended_events)
        self.assertEqual(repository.contents["portfolio/audit.md"], delivery["row"])

    def test_preview_requires_exact_row_but_never_patches_missing_event(self):
        delivery = synthetic_delivery()
        repository = FakeRepository(delivery["row"], "")
        with (
            patch.dict(os.environ, outbox_environment(), clear=True),
            patch.object(
                recovery.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            status = recovery._inspect_and_maybe_publish_event(
                delivery, publish=False
            )

        self.assertEqual(status, "event_missing")
        self.assertEqual(repository.update_calls, [])

    def test_workflow_uses_secret_ids_and_both_reviewed_hashes(self):
        workflow = (
            Path(__file__).parents[1]
            / ".github"
            / "workflows"
            / "recover_ghostfolio_event.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("expected_crypto_order_id:", workflow)
        self.assertNotIn("expected_funding_order_id:", workflow)
        self.assertNotIn("inputs.expected_crypto_order_id", workflow)
        self.assertNotIn("inputs.expected_funding_order_id", workflow)
        self.assertIn("GHOSTFOLIO_RECOVERY_CRYPTO_ORDER_ID", workflow)
        self.assertIn("GHOSTFOLIO_RECOVERY_FUNDING_ORDER_ID", workflow)
        self.assertIn("expected_row_sha256:", workflow)
        self.assertIn("--expected-row-sha256", workflow)
        self.assertLess(workflow.index("safe_order_id="), workflow.index("::add-mask"))
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", workflow)

    def test_cli_prints_only_safe_summary_and_masks_third_party_error_text(self):
        argv = [
            "--target",
            "HYPE_USD",
            "--trade-date",
            "2026-08-07",
            "--budget-gbp",
            "12.50",
            "--expected-crypto-order-id",
            CRYPTO_ORDER_ID,
            "--expected-funding-order-id",
            FUNDING_ORDER_ID,
        ]
        stdout = io.StringIO()
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                return_value=reconciled_fill(),
            ),
            patch.object(
                recovery,
                "_inspect_and_maybe_publish_event",
                return_value="event_missing",
            ),
            redirect_stdout(stdout),
        ):
            self.assertEqual(recovery.main(argv), 0)

        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["target"], "HYPE_USD")
        self.assertNotIn("amount_crypto", printed)
        self.assertNotIn("fee_details", printed)
        self.assertNotIn("funded_usd", printed)

        stderr = io.StringIO()
        with (
            patch.object(
                recovery,
                "place_gbp_funded_market_buy",
                side_effect=RuntimeError("KRAKEN_SECRET_SENTINEL"),
            ),
            patch.object(recovery, "_inspect_and_maybe_publish_event"),
            redirect_stderr(stderr),
        ):
            self.assertEqual(recovery.main(argv), 2)

        self.assertNotIn("KRAKEN_SECRET_SENTINEL", stderr.getvalue())
        self.assertIn("RuntimeError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

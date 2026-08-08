import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gist_logger
from github_contents import GitHubContentsError, RepositoryFile, WriteResult


def event_content(delivery):
    return json.dumps(
        delivery["event"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ) + "\n"


class FakeRepository:
    def __init__(self, contents=None, fail_path=None):
        self.contents = dict(contents or {})
        self.fail_path = fail_path
        self.calls = []

    def read_text(self, path):
        content = self.contents.get(path, "")
        return RepositoryFile(content=content, sha="a" * 40, exists=path in self.contents)

    def update_text(self, path, transform, *, message):
        self.calls.append((path, message))
        if path == self.fail_path:
            raise GitHubContentsError("sanitized transport failure")
        current = self.contents.get(path, "")
        updated = transform(current)
        self.contents[path] = updated
        return WriteResult(
            changed=updated != current,
            sha="a" * 40,
            content=updated,
        )


class RepositoryLoggerTests(unittest.TestCase):
    def setUp(self):
        self.trade = {
            "ts": 1783830882,
            "quote_currency": "USD",
            "amount_gbp": 10.0,
            "cost_gbp": 9.95,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.0,
            "gbp_usd_rate": 1.25,
            "funded_usd": 12.48,
            "funding_fee_usd": 0.02,
            "cost_usd": 12.47,
            "fee_usd": 0.03,
            "usd_fee_debit": 0.0,
            "fee_details": [
                {"currency": "BTC", "amount": 0.0000005, "gbp_equivalent": 0.03},
                {"currency": "USD", "amount": 0.02, "gbp_equivalent": 0.02},
            ],
            "amount_crypto": 0.0002,
            "usd_price_per_unit": 62_350.0,
            "funding_order_id": "FX-123",
            "order_id": "KRAKEN-123",
        }
        self.environment = {
            "DCA_OUTBOX_REPOSITORY_OWNER": "example",
            "DCA_OUTBOX_REPOSITORY_NAME": "private-outbox",
            "DCA_OUTBOX_REPOSITORY_BRANCH": "main",
            "DCA_OUTBOX_REPOSITORY_TOKEN": "token",
            "DCA_OUTBOX_AUDIT_PATH": "portfolio/audit.md",
            "DCA_OUTBOX_EVENT_PATH": "portfolio/events.jsonl",
            "DCA_OUTBOX_HOLDINGS_PATH": "portfolio/holdings.json",
        }

    def _publish(self, repository, value=None, **kwargs):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")),
            patch.object(
                gist_logger.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
        ):
            return gist_logger.update_gist_log(value or self.trade, **kwargs)

    def test_publishes_audit_and_event_to_only_configured_paths(self):
        repository = FakeRepository({"unrelated.md": "leave me alone"})
        saved = self._publish(
            repository,
            symbol="BTC",
            saved_to_ghostfolio=True,
        )

        self.assertTrue(saved)
        self.assertEqual(
            [call[0] for call in repository.calls],
            ["portfolio/audit.md", "portfolio/events.jsonl"],
        )
        content = repository.contents["portfolio/audit.md"]
        self.assertIn("# Kraken USD DCA Trade Log", content)
        self.assertIn("GBP 10.00", content)
        self.assertIn("USD 12.4800", content)
        self.assertIn("KRAKEN-123", content)
        self.assertIn("| yes |", content)
        self.assertEqual(
            json.loads(repository.contents["portfolio/events.jsonl"]),
            gist_logger.build_gist_delivery(self.trade, "BTC", True)["event"],
        )
        self.assertEqual(repository.contents["unrelated.md"], "leave me alone")

    def test_appends_without_replacing_existing_header(self):
        existing = "# Kraken USD DCA Trade Log\n\nexisting row"
        repository = FakeRepository({"portfolio/audit.md": existing})

        self.assertTrue(self._publish(repository, symbol="BTC"))

        content = repository.contents["portfolio/audit.md"]
        self.assertTrue(content.startswith(existing + "\n"))
        self.assertEqual(content.count("# Kraken USD DCA Trade Log"), 1)
        self.assertIn("| optional/not saved |", content)

    def test_build_delivery_is_deterministic_compact_and_markdown_safe(self):
        trade = {
            **self.trade,
            "funding_order_id": "fx|id\nnext",
            "order_id": "abc|def\nnext",
        }
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            first = gist_logger.build_gist_delivery(trade, "btc", True)
            second = gist_logger.build_gist_delivery(dict(trade), "BTC", True)

        self.assertEqual(first, second)
        self.assertEqual(first["version"], 3)
        self.assertEqual(first["delivery_id"], "abc|def next")
        self.assertEqual(first["created_at"], "2026-07-12T04:34:42Z")
        self.assertEqual(
            first["row_sha256"],
            hashlib.sha256(first["row"].encode("utf-8")).hexdigest(),
        )
        self.assertIn(r"fx\|id next", first["row"])
        self.assertIn(r"abc\|def next", first["row"])

    def test_build_delivery_rejects_missing_or_invalid_order_ids(self):
        invalid_trades = [
            {key: value for key, value in self.trade.items() if key != "order_id"},
            {**self.trade, "order_id": "  "},
            {**self.trade, "order_id": "unknown"},
            {**self.trade, "order_id": 123},
            {
                key: value
                for key, value in self.trade.items()
                if key != "funding_order_id"
            },
        ]
        for invalid_trade in invalid_trades:
            with self.subTest(order_id=invalid_trade.get("order_id")):
                with self.assertRaises(ValueError):
                    gist_logger.build_gist_delivery(invalid_trade, "BTC")

    def test_identical_existing_delivery_is_idempotent(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        repository = FakeRepository({
            "portfolio/audit.md": gist_logger.TABLE_HEADER + delivery["row"],
            "portfolio/events.jsonl": event_content(delivery),
        })

        self.assertTrue(self._publish(repository, delivery))
        self.assertEqual(
            repository.contents["portfolio/audit.md"].count(delivery["row"]),
            1,
        )
        self.assertEqual(
            repository.contents["portfolio/events.jsonl"].count(event_content(delivery)),
            1,
        )

    def test_same_order_id_with_different_row_is_rejected(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
            conflicting = gist_logger.build_gist_delivery(
                {**self.trade, "amount_crypto": 0.0003}, "BTC"
            )
        repository = FakeRepository({
            "portfolio/audit.md": gist_logger.TABLE_HEADER + conflicting["row"],
        })

        self.assertFalse(self._publish(repository, delivery))
        self.assertNotIn("portfolio/events.jsonl", repository.contents)

    def test_partial_failure_keeps_retry_idempotent(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        repository = FakeRepository(fail_path="portfolio/events.jsonl")

        self.assertFalse(self._publish(repository, delivery))
        self.assertEqual(
            repository.contents["portfolio/audit.md"].count(delivery["row"]), 1
        )
        repository.fail_path = None
        self.assertTrue(self._publish(repository, delivery))
        self.assertEqual(
            repository.contents["portfolio/audit.md"].count(delivery["row"]), 1
        )
        self.assertEqual(
            repository.contents["portfolio/events.jsonl"].count(event_content(delivery)),
            1,
        )

    def test_size_limit_refuses_before_event_write(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        repository = FakeRepository()
        with patch.object(gist_logger, "MAX_GIST_FILE_BYTES", 1):
            self.assertFalse(self._publish(repository, delivery))
        self.assertNotIn("portfolio/events.jsonl", repository.contents)

    def test_invalid_trade_is_rejected_before_client_construction(self):
        invalid = {**self.trade, "quote_currency": "EUR"}
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(gist_logger.GitHubContentsClient, "from_env") as constructor,
        ):
            self.assertFalse(gist_logger.update_gist_log(invalid))
        constructor.assert_not_called()

    def test_missing_repository_config_and_gist_only_config_fail_closed(self):
        for environment in ({}, {"GIST_ID": "legacy", "GIST_TOKEN": "legacy-token"}):
            with self.subTest(environment=tuple(environment)):
                with patch.dict(os.environ, environment, clear=True):
                    self.assertFalse(gist_logger.update_gist_log(self.trade))

    def test_failure_output_never_contains_token_or_payload(self):
        token = "github_pat_SECRET_TOKEN"
        payload = "PAYLOAD_SECRET_SENTINEL"
        environment = {**self.environment, "DCA_OUTBOX_REPOSITORY_TOKEN": token}
        repository = FakeRepository()

        def fail(*_args, **_kwargs):
            raise GitHubContentsError(payload)

        repository.update_text = fail
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                gist_logger.GitHubContentsClient,
                "from_env",
                return_value=repository,
            ),
            redirect_stdout(output),
        ):
            self.assertFalse(gist_logger.update_gist_log(self.trade))
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(payload, output.getvalue())
        self.assertIn("GitHubContentsError", output.getvalue())


if __name__ == "__main__":
    unittest.main()

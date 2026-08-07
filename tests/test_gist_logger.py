import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import gist_logger


def event_content(delivery):
    return json.dumps(
        delivery["event"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ) + "\n"


class MockResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class GistLoggerTests(unittest.TestCase):
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
                {
                    "currency": "BTC",
                    "amount": 0.0000005,
                    "gbp_equivalent": 0.03,
                },
                {
                    "currency": "USD",
                    "amount": 0.02,
                    "gbp_equivalent": 0.02,
                },
            ],
            "amount_crypto": 0.0002,
            "usd_price_per_unit": 62_350.0,
            "funding_order_id": "FX-123",
            "order_id": "KRAKEN-123",
        }

    def _credentials(self):
        return patch.multiple(
            gist_logger,
            GIST_ID="gist-id",
            GIST_TOKEN="gist-token",
            SELECTED_TZ=ZoneInfo("UTC"),
        )

    def test_creates_dedicated_usd_dca_file_and_returns_true(self):
        get_response = MockResponse(
            body={"files": {"unrelated.md": {"content": "leave me alone"}}}
        )

        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get", return_value=get_response) as get,
            patch.object(
                gist_logger.requests, "patch", return_value=MockResponse()
            ) as patch_request,
        ):
            saved = gist_logger.update_gist_log(
                self.trade, symbol="BTC", saved_to_ghostfolio=True
            )

        self.assertTrue(saved)
        get.assert_called_once()
        self.assertEqual(
            get.call_args.kwargs["timeout"],
            gist_logger.GIST_REQUEST_TIMEOUT_SECONDS,
        )
        payload_files = patch_request.call_args.kwargs["json"]["files"]
        self.assertEqual(
            list(payload_files),
            [gist_logger.GIST_FILENAME, gist_logger.GHOSTFOLIO_EVENTS_FILENAME],
        )
        content = payload_files[gist_logger.GIST_FILENAME]["content"]
        self.assertIn("# Kraken USD DCA Trade Log", content)
        self.assertIn("Kraken is the source of truth", content)
        self.assertIn("GBP 10.00", content)
        self.assertIn("1.250000", content)
        self.assertIn("USD 12.4800", content)
        self.assertIn("USD 12.4700", content)
        self.assertIn("0.00000050 BTC (GBP equiv 0.03)", content)
        self.assertIn("USD 62,350.0000", content)
        self.assertIn("0.00020000 BTC", content)
        self.assertIn("FX-123", content)
        self.assertIn("KRAKEN-123", content)
        self.assertIn("| yes |", content)
        self.assertNotIn("unrelated.md", content)

    def test_appends_without_replacing_existing_header(self):
        existing = "# Kraken USD DCA Trade Log\n\nexisting row"
        get_response = MockResponse(
            body={"files": {gist_logger.GIST_FILENAME: {"content": existing}}}
        )

        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get", return_value=get_response),
            patch.object(
                gist_logger.requests, "patch", return_value=MockResponse()
            ) as patch_request,
        ):
            saved = gist_logger.update_gist_log(self.trade, symbol="BTC")

        self.assertTrue(saved)
        content = patch_request.call_args.kwargs["json"]["files"][
            gist_logger.GIST_FILENAME
        ]["content"]
        self.assertTrue(content.startswith(existing + "\n"))
        self.assertEqual(content.count("# Kraken USD DCA Trade Log"), 1)
        self.assertIn("| optional/not saved |", content)

    def test_order_identifiers_are_safe_for_markdown_table(self):
        trade = {
            **self.trade,
            "funding_order_id": "fx|id\nnext",
            "order_id": "abc|def\nnext",
        }

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(body={"files": {}}),
            ),
            patch.object(
                gist_logger.requests, "patch", return_value=MockResponse()
            ) as patch_request,
        ):
            saved = gist_logger.update_gist_log(trade)

        self.assertTrue(saved)
        content = patch_request.call_args.kwargs["json"]["files"][
            gist_logger.GIST_FILENAME
        ]["content"]
        self.assertIn(r"fx\|id next", content)
        self.assertIn(r"abc\|def next", content)

    def test_build_delivery_is_deterministic_and_compact(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            first = gist_logger.build_gist_delivery(
                self.trade, "btc", saved_to_ghostfolio=True
            )
            second = gist_logger.build_gist_delivery(
                dict(self.trade), "BTC", saved_to_ghostfolio=True
            )

        self.assertEqual(first, second)
        self.assertEqual(
            list(first),
            [
                "version",
                "delivery_id",
                "created_at",
                "symbol",
                "row",
                "row_sha256",
                "event",
                "event_sha256",
            ],
        )
        self.assertEqual(first["version"], 2)
        self.assertEqual(first["delivery_id"], "KRAKEN-123")
        self.assertEqual(first["created_at"], "2026-07-12T04:34:42Z")
        self.assertEqual(first["symbol"], "BTC")
        self.assertEqual(
            first["row_sha256"],
            hashlib.sha256(first["row"].encode("utf-8")).hexdigest(),
        )

    def test_build_delivery_rejects_missing_or_invalid_order_ids(self):
        invalid_trades = [
            {
                key: value
                for key, value in self.trade.items()
                if key != "order_id"
            },
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

    def test_identical_existing_delivery_is_success_without_patch(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        existing = gist_logger.TABLE_HEADER + delivery["row"]

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(
                    body={
                        "files": {
                            gist_logger.GIST_FILENAME: {"content": existing},
                            gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                                "content": event_content(delivery)
                            },
                        }
                    }
                ),
            ),
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(delivery)

        self.assertTrue(saved)
        patch_request.assert_not_called()

    def test_same_order_id_with_different_row_is_rejected(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
            conflicting_delivery = gist_logger.build_gist_delivery(
                {**self.trade, "amount_crypto": 0.0003}, "BTC"
            )
        existing = gist_logger.TABLE_HEADER + conflicting_delivery["row"]

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(
                    body={
                        "files": {
                            gist_logger.GIST_FILENAME: {"content": existing},
                            gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                                "content": event_content(delivery)
                            },
                        }
                    }
                ),
            ),
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(delivery)

        self.assertFalse(saved)
        patch_request.assert_not_called()

    def test_lost_patch_response_can_be_retried_without_duplicate(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        remote_content = {"markdown": "", "events": ""}

        def get_remote(*args, **kwargs):
            return MockResponse(
                body={
                    "files": {
                        gist_logger.GIST_FILENAME: {
                            "content": remote_content["markdown"]
                        },
                        gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                            "content": remote_content["events"]
                        },
                    }
                }
            )

        def patch_then_lose_response(*args, **kwargs):
            remote_content["markdown"] = kwargs["json"]["files"][
                gist_logger.GIST_FILENAME
            ]["content"]
            remote_content["events"] = kwargs["json"]["files"][
                gist_logger.GHOSTFOLIO_EVENTS_FILENAME
            ]["content"]
            raise requests.ConnectionError("response was lost")

        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get", side_effect=get_remote),
            patch.object(
                gist_logger.requests,
                "patch",
                side_effect=patch_then_lose_response,
            ) as patch_request,
        ):
            first_result = gist_logger.update_gist_log(delivery)
            second_result = gist_logger.update_gist_log(delivery)

        self.assertFalse(first_result)
        self.assertTrue(second_result)
        self.assertEqual(patch_request.call_count, 1)
        self.assertEqual(remote_content["markdown"].count(delivery["row"]), 1)

    def test_truncated_api_content_fetches_complete_raw_file_before_patch(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        historical_row = delivery["row"].replace("KRAKEN-123", "KRAKEN-OLD")
        complete_content = gist_logger.TABLE_HEADER + historical_row
        api_response = MockResponse(
            body={
                "files": {
                    gist_logger.GIST_FILENAME: {
                        "content": gist_logger.TABLE_HEADER,
                        "truncated": True,
                        "raw_url": (
                            "https://gist.githubusercontent.com/user/gist/raw/sha/"
                            + gist_logger.GIST_FILENAME
                        ),
                    }
                }
            }
        )
        raw_response = MockResponse(body=complete_content)

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests, "get", side_effect=[api_response, raw_response]
            ) as get_request,
            patch.object(
                gist_logger.requests, "patch", return_value=MockResponse()
            ) as patch_request,
        ):
            self.assertTrue(gist_logger.update_gist_log(delivery))

        self.assertEqual(get_request.call_count, 2)
        patched_content = patch_request.call_args.kwargs["json"]["files"][
            gist_logger.GIST_FILENAME
        ]["content"]
        self.assertTrue(patched_content.startswith(complete_content))
        self.assertIn(historical_row, patched_content)
        self.assertTrue(patched_content.endswith(delivery["row"]))

    def test_truncated_file_refuses_unsafe_raw_url_without_sending_token(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        api_response = MockResponse(
            body={
                "files": {
                    gist_logger.GIST_FILENAME: {
                        "content": gist_logger.TABLE_HEADER,
                        "truncated": True,
                        "raw_url": "https://example.invalid/stolen-ledger",
                    }
                }
            }
        )

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests, "get", return_value=api_response
            ) as get_request,
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            self.assertFalse(gist_logger.update_gist_log(delivery))

        self.assertEqual(get_request.call_count, 1)
        patch_request.assert_not_called()

    def test_append_refuses_to_cross_supported_gist_file_size(self):
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(self.trade, "BTC")
        existing = gist_logger.TABLE_HEADER
        with (
            self._credentials(),
            patch.object(
                gist_logger, "MAX_GIST_FILE_BYTES", len(existing.encode("utf-8"))
            ),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(
                    body={
                        "files": {
                            gist_logger.GIST_FILENAME: {"content": existing},
                            gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                                "content": event_content(delivery)
                            },
                        }
                    }
                ),
            ),
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            self.assertFalse(gist_logger.update_gist_log(delivery))

        patch_request.assert_not_called()

    def test_escaped_pipe_ids_are_matched_in_exact_order_column(self):
        pipe_trade = {
            **self.trade,
            "funding_order_id": "FX|123",
            "order_id": "KRAKEN|123",
        }
        with patch.object(gist_logger, "SELECTED_TZ", ZoneInfo("UTC")):
            delivery = gist_logger.build_gist_delivery(pipe_trade, "BTC")
        existing = gist_logger.TABLE_HEADER + delivery["row"]

        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(
                    body={
                        "files": {
                            gist_logger.GIST_FILENAME: {"content": existing},
                            gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                                "content": event_content(delivery)
                            },
                        }
                    }
                ),
            ),
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(delivery)

        self.assertTrue(saved)
        self.assertIn(r"KRAKEN\|123", delivery["row"])
        patch_request.assert_not_called()

    def test_rejects_non_usd_trade_before_network_request(self):
        invalid_trade = {**self.trade, "quote_currency": "GBP"}
        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get") as get,
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(invalid_trade)

        self.assertFalse(saved)
        get.assert_not_called()
        patch_request.assert_not_called()

    def test_rejects_crypto_debit_above_confirmed_usd(self):
        invalid_trade = {
            **self.trade,
            "cost_usd": 12.48,
            "usd_fee_debit": 0.02,
        }
        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get") as get,
        ):
            saved = gist_logger.update_gist_log(invalid_trade)

        self.assertFalse(saved)
        get.assert_not_called()

    def test_missing_credentials_skips_all_requests(self):
        with (
            patch.object(gist_logger, "GIST_ID", None),
            patch.object(gist_logger, "GIST_TOKEN", None),
            patch.object(gist_logger.requests, "get") as get,
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(self.trade)

        self.assertFalse(saved)
        get.assert_not_called()
        patch_request.assert_not_called()

    def test_get_http_failure_returns_false_without_patch(self):
        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(status_code=503),
            ),
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(self.trade)

        self.assertFalse(saved)
        patch_request.assert_not_called()

    def test_patch_http_failure_returns_false(self):
        with (
            self._credentials(),
            patch.object(
                gist_logger.requests,
                "get",
                return_value=MockResponse(body={"files": {}}),
            ),
            patch.object(
                gist_logger.requests,
                "patch",
                return_value=MockResponse(status_code=422),
            ),
        ):
            saved = gist_logger.update_gist_log(self.trade)

        self.assertFalse(saved)

    def test_request_failure_does_not_print_secret_url_or_token(self):
        secret_url = "https://api.github.com/gists/private-gist-secret"
        secret_token = "github_pat_private_secret"
        output = io.StringIO()
        with (
            patch.multiple(
                gist_logger,
                GIST_ID="private-gist-secret",
                GIST_TOKEN=secret_token,
                SELECTED_TZ=ZoneInfo("UTC"),
            ),
            patch.object(
                gist_logger.requests,
                "get",
                side_effect=requests.RequestException(
                    f"request to {secret_url} with {secret_token} failed"
                ),
            ),
            redirect_stdout(output),
        ):
            saved = gist_logger.update_gist_log(self.trade)

        self.assertFalse(saved)
        self.assertNotIn(secret_url, output.getvalue())
        self.assertNotIn(secret_token, output.getvalue())
        self.assertIn("RequestException", output.getvalue())


if __name__ == "__main__":
    unittest.main()

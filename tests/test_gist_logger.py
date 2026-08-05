import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import gist_logger


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
            "amount_gbp": 10.0,
            "cost_gbp": 9.95,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.05,
            "fee_details": [
                {
                    "currency": "GBP",
                    "amount": 0.05,
                    "gbp_equivalent": 0.05,
                }
            ],
            "amount_crypto": 0.0002,
            "gbp_price_per_unit": 50000.0,
            "order_id": "KRAKEN-123",
        }

    def _credentials(self):
        return patch.multiple(
            gist_logger,
            GIST_ID="gist-id",
            GIST_TOKEN="gist-token",
            SELECTED_TZ=ZoneInfo("UTC"),
        )

    def test_creates_dedicated_gbp_file_and_returns_true(self):
        get_response = MockResponse(
            body={"files": {"unrelated.md": {"content": "leave me alone"}}}
        )
        patch_response = MockResponse(status_code=200)

        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get", return_value=get_response) as get,
            patch.object(
                gist_logger.requests, "patch", return_value=patch_response
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
        self.assertEqual(list(payload_files), [gist_logger.GIST_FILENAME])
        content = payload_files[gist_logger.GIST_FILENAME]["content"]
        self.assertIn("# Kraken GBP Trade Log", content)
        self.assertIn("GBP 9.95", content)
        self.assertIn("0.05000000 GBP (GBP equiv 0.05)", content)
        self.assertIn("GBP 10.00", content)
        self.assertIn("GBP 50,000.0000", content)
        self.assertIn("0.00020000 BTC", content)
        self.assertIn("KRAKEN-123", content)
        self.assertIn("| yes |", content)
        self.assertNotIn("unrelated.md", content)
        self.assertEqual(
            patch_request.call_args.kwargs["timeout"],
            gist_logger.GIST_REQUEST_TIMEOUT_SECONDS,
        )

    def test_appends_to_existing_dedicated_file_without_replacing_header(self):
        existing = "# Kraken GBP Trade Log\n\nexisting row"
        get_response = MockResponse(
            body={
                "files": {
                    gist_logger.GIST_FILENAME: {"content": existing}
                }
            }
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
        self.assertEqual(content.count("# Kraken GBP Trade Log"), 1)
        self.assertIn("| no |", content)

    def test_order_identifier_is_safe_for_markdown_table(self):
        trade = {**self.trade, "order_id": "abc|def\nnext"}

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
        self.assertIn(r"abc\|def next", content)

    def test_base_asset_fee_keeps_total_to_gbp_cash_debit(self):
        trade = {
            **self.trade,
            "amount_gbp": 9.95,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.0,
            "fee_details": [
                {
                    "currency": "BTC",
                    "amount": 0.000001,
                    "gbp_equivalent": 0.05,
                }
            ],
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
            saved = gist_logger.update_gist_log(trade, symbol="BTC")

        self.assertTrue(saved)
        content = patch_request.call_args.kwargs["json"]["files"][
            gist_logger.GIST_FILENAME
        ]["content"]
        self.assertIn("0.00000100 BTC (GBP equiv 0.05)", content)
        self.assertIn("| GBP 9.95 | 0.00000100 BTC", content)
        self.assertIn("| GBP 9.95 | GBP 50,000.0000 |", content)

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

    def test_invalid_gbp_trade_fails_closed_before_network_request(self):
        invalid_trade = {**self.trade, "amount_gbp": 0}
        with (
            self._credentials(),
            patch.object(gist_logger.requests, "get") as get,
            patch.object(gist_logger.requests, "patch") as patch_request,
        ):
            saved = gist_logger.update_gist_log(invalid_trade)

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

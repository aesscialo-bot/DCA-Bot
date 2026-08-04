import json
import unittest
from unittest.mock import ANY, MagicMock, patch

import crypto_analysis


class AnalysisSymbolTests(unittest.TestCase):
    def test_analysis_exchange_is_kraken_only(self):
        with self.assertRaisesRegex(ValueError, "Kraken GBP markets only"):
            crypto_analysis.get_analysis_exchange("coinbase")

    def test_derives_kraken_gbp_pairs_from_config(self):
        target_map = {
            "BTC_GBP": {"TIME": "02:45"},
            "LINK_GBP": {"TIME": "03:00"},
        }
        self.assertEqual(
            crypto_analysis._parse_symbols("", json.dumps(target_map)),
            ["BTC/GBP", "LINK/GBP"],
        )

    def test_explicit_plain_and_underscore_symbols_become_gbp_pairs(self):
        self.assertEqual(
            crypto_analysis._parse_symbols("BTC,LINK_GBP", "{}"),
            ["BTC/GBP", "LINK/GBP"],
        )

    def test_non_gbp_explicit_pair_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Only GBP"):
            crypto_analysis._parse_symbols("BTC/USD", "{}")

    def test_noncanonical_map_is_rejected_without_reinterpreting_amounts(self):
        with self.assertRaisesRegex(ValueError, "no canonical COIN_GBP"):
            crypto_analysis._parse_symbols("", '{"BTC_OLD":{"TIME":"02:45"}}')
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            crypto_analysis._parse_symbols("", "not-json")

    def test_empty_sources_fail_instead_of_inventing_a_default_market(self):
        with self.assertRaisesRegex(ValueError, "DCA_TARGET_MAP is empty"):
            crypto_analysis._parse_symbols("", "{}")

    def test_ai_summary_uses_supported_genai_client_and_flash_lite(self):
        response = MagicMock()
        response.text = "RECOMMENDED_TIME: 02:45\nREASON: lowest median miss"
        client = MagicMock()
        client.__enter__.return_value = client
        client.models.generate_content.return_value = response

        with (
            patch.object(crypto_analysis, "GEMINI_API_KEY", "test-key"),
            patch.object(crypto_analysis.genai, "Client", return_value=client) as ctor,
        ):
            summary, selected_time, model = crypto_analysis.get_ai_summary(
                "report", "BTC/GBP"
            )

        ctor.assert_called_once_with(api_key="test-key")
        client.models.generate_content.assert_called_once_with(
            model="gemini-2.5-flash-lite",
            contents=ANY,
        )
        self.assertEqual(selected_time, "02:45")
        self.assertEqual(model, "gemini-2.5-flash-lite")
        self.assertIn("RECOMMENDED_TIME", summary)


if __name__ == "__main__":
    unittest.main()

from unittest.mock import patch
import unittest

import crypto_dca
from dca_config import TARGET_ROUTES, TARGET_SYMBOLS


class MixedRouteTests(unittest.TestCase):
    def test_production_market_contract_is_gbp_native_and_explicit(self):
        self.assertEqual(TARGET_SYMBOLS, {
            "BTC_GBP": "BTC/GBP",
            "ETH_GBP": "ETH/GBP",
            "SOL_GBP": "SOL/GBP",
        })
        self.assertEqual(TARGET_ROUTES, {
            "BTC_GBP": "DIRECT_GBP",
            "ETH_GBP": "DIRECT_GBP",
            "SOL_GBP": "DIRECT_GBP",
        })

    def test_native_gbp_route_uses_exactly_one_connector_call(self):
        with (
            patch.object(crypto_dca, "place_market_buy", return_value={"ok": True}) as direct,
            patch.object(crypto_dca, "place_gbp_funded_market_buy") as funded,
        ):
            result = crypto_dca._place_routed_market_buy(
                "SOL_GBP", 15, client_order_id="buy", funding_client_order_id="unused",
                reconcile_only=False, pre_submit_check=None,
            )
        self.assertEqual(result, {"ok": True})
        direct.assert_called_once()
        funded.assert_not_called()
        self.assertNotIn("funding_client_order_id", direct.call_args.kwargs)

    def test_eth_gbp_route_uses_exactly_one_direct_connector_call(self):
        with (
            patch.object(crypto_dca, "place_market_buy", return_value={"ok": True}) as direct,
            patch.object(crypto_dca, "place_gbp_funded_market_buy") as funded,
        ):
            result = crypto_dca._place_routed_market_buy(
                "ETH_GBP", 12.5, client_order_id="buy", funding_client_order_id="unused",
                reconcile_only=False, pre_submit_check=None,
            )
        self.assertEqual(result, {"ok": True})
        direct.assert_called_once()
        funded.assert_not_called()
        self.assertNotIn("funding_client_order_id", direct.call_args.kwargs)

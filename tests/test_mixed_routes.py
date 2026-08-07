from unittest.mock import patch

import crypto_dca
from dca_config import TARGET_ROUTES, TARGET_SYMBOLS


def test_production_market_contract_is_mixed_and_explicit():
    assert TARGET_SYMBOLS == {
        "BTC_GBP": "BTC/GBP",
        "HYPE_USD": "HYPE/USD",
        "SOL_GBP": "SOL/GBP",
    }
    assert TARGET_ROUTES == {
        "BTC_GBP": "DIRECT_GBP",
        "HYPE_USD": "GBP_TO_USD",
        "SOL_GBP": "DIRECT_GBP",
    }


def test_native_gbp_route_uses_exactly_one_connector_call():
    with (
        patch.object(crypto_dca, "place_market_buy", return_value={"ok": True}) as direct,
        patch.object(crypto_dca, "place_gbp_funded_market_buy") as funded,
    ):
        result = crypto_dca._place_routed_market_buy(
            "SOL_GBP", 15, client_order_id="buy", funding_client_order_id="unused",
            reconcile_only=False, pre_submit_check=None,
        )
    assert result == {"ok": True}
    direct.assert_called_once()
    funded.assert_not_called()
    assert "funding_client_order_id" not in direct.call_args.kwargs


def test_hype_usd_route_uses_confirmed_gbp_funding_connector():
    with (
        patch.object(crypto_dca, "place_market_buy") as direct,
        patch.object(crypto_dca, "place_gbp_funded_market_buy", return_value={"ok": True}) as funded,
    ):
        result = crypto_dca._place_routed_market_buy(
            "HYPE_USD", 12.5, client_order_id="buy", funding_client_order_id="funding",
            reconcile_only=False, pre_submit_check=None,
        )
    assert result == {"ok": True}
    direct.assert_not_called()
    funded.assert_called_once()
    assert funded.call_args.kwargs["funding_client_order_id"] == "funding"

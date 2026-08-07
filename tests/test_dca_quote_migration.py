import json
from datetime import datetime, timezone

import pytest

from dca_quote_migration import migrate


NOW = datetime(2026, 8, 7, 1, 0, tzinfo=timezone.utc)


def old_rules():
    return {
        key: {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        for key in ("BTC_USD", "HYPE_USD", "SOL_USD")
    }


def test_migration_preserves_rules_and_last_buy_dates_but_resets_analysis():
    execution = {
        "BTC_USD": {"LAST_BUY_DATE": "2026-08-06"},
        "HYPE_USD": {"LAST_BUY_DATE": ""},
        "SOL_USD": {"LAST_BUY_DATE": "2026-08-05"},
    }
    result = migrate(json.dumps(old_rules()), json.dumps(execution), now=NOW)
    assert set(result["DCA_TARGET_MAP"]) == {"BTC_GBP", "HYPE_USD", "SOL_GBP"}
    assert result["DCA_EXECUTION_STATE"]["BTC_GBP"]["LAST_BUY_DATE"] == "2026-08-06"
    assert result["DCA_EXECUTION_STATE"]["SOL_GBP"]["LAST_BUY_DATE"] == "2026-08-05"
    assert all(
        row["ANALYSIS_STATUS"] == "AWAITING_ANALYSIS"
        for row in result["DCA_ANALYSIS_STATE"]["TARGETS"].values()
    )
    assert result["DCA_TRADING_MODE"] == "shadow"
    assert result["DCA_CANARY_SYMBOL"] == "SOL_GBP"


@pytest.mark.parametrize("field", ["PENDING_ORDER", "PENDING_GIST_DELIVERIES"])
def test_migration_blocks_unresolved_btc_or_sol_evidence(field):
    value = {"BTC_USD": {"LAST_BUY_DATE": ""}}
    value["BTC_USD"][field] = {"anything": True} if field == "PENDING_ORDER" else [{}]
    with pytest.raises(ValueError, match="before migration"):
        migrate(json.dumps(old_rules()), json.dumps(value), now=NOW)

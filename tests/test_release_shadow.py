"""Full Bangkok-day execution simulation: fixture state only, no network/order IO."""
import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_dca
import dca_config
import kraken_client
from tests.test_crypto_dca import analysis_for, ready_decision


class ReleaseShadowTests(unittest.TestCase):
    def test_full_day_due_enabled_targets_in_shadow_never_create_intents_or_orders(self):
        rules = dca_config.default_rules_map()
        for target in rules:
            rules[target] = {
                "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 15},
                "BUY_ENABLED": True,
            }
        start = datetime(2026, 9, 4, 17, tzinfo=timezone.utc)
        analysis_at = start + timedelta(hours=4, minutes=7)
        state = {target: {"LAST_BUY_DATE": "2026-09-04"} for target in rules}
        prior = analysis_for(rules, {}, now=analysis_at - timedelta(days=1))
        decisions = {
            target: ready_decision(target, rules[target], now=analysis_at,
                                   offset=60 + index * 180)
            for index, target in enumerate(rules)
        }
        current = analysis_for(rules, decisions, now=analysis_at)
        # A decision for each of the four targets becomes due during this day.
        shadow_seen = set()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(crypto_dca, "DCA_TRADING_MODE", "shadow"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_initial_execution_state", return_value=state),
            patch.object(crypto_dca, "_initial_rules_map", return_value=rules),
            patch.object(crypto_dca, "_configured_start_date", return_value=start.date()),
            patch.object(crypto_dca, "retry_pending_gist_deliveries", return_value=True),
            patch.object(crypto_dca, "send_discord_alert"),
            patch.object(crypto_dca, "execute_trade") as execute,
            patch.object(crypto_dca, "_write_repo_json_variable") as write_state,
            patch.object(kraken_client, "get_kraken_exchange") as exchange,
        ):
            for minute in range(0, 24 * 60, 5):
                now = start + timedelta(minutes=minute)
                analysis = prior if now < analysis_at else current
                with (
                    patch.object(crypto_dca, "_utc_now", return_value=now),
                    patch.object(crypto_dca, "_initial_analysis_state", return_value=analysis),
                ):
                    self.assertTrue(crypto_dca.main())
                    # Duplicate dispatches/restarts must be equally harmless.
                    self.assertTrue(crypto_dca.main())
                if now >= analysis_at:
                    for target, decision in decisions.items():
                        if crypto_dca._decision_gate(target, rules[target], decision, now)[0] == "SHADOW":
                            shadow_seen.add(target)
            self.assertEqual(shadow_seen, set(rules))
            execute.assert_not_called()
            write_state.assert_not_called()
            exchange.assert_not_called()
        self.assertEqual(state, {target: {"LAST_BUY_DATE": "2026-09-04"} for target in rules})


if __name__ == "__main__":
    unittest.main()

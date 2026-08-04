# CLAUDE.md

Repository guidance for automated coding agents.

## System contract

This is a Python 3.12, Kraken Spot, GBP-native DCA system.

- Configuration keys are canonical `COIN_GBP` values.
- The configured budget field is `AMOUNT_GBP` and is already denominated in pounds.
- The execution symbol is `COIN/GBP`.
- Do not add an exchange switch, quote-currency switch, or budget FX conversion to the trading path.
- Reject unsupported configuration rather than translating it.
- Keep `BUY_ENABLED=false` during development, tests, deployment, migration, and read-only live checks.

## Runtime and ownership

- Railway runs `discord_bot.py` continuously. It reads repository variables, ticks on a five-minute clock, and dispatches workflows; it does not hold Kraken credentials.
- GitHub Actions runs analysis, trading, serialized configuration writes, and portfolio reporting.
- Kraken is the only trading and portfolio exchange.
- `DCA_TARGET_MAP` is user-managed desired configuration. It contains rules only.
- `DCA_EXECUTION_STATE` is trader-owned runtime state. It contains completion dates and unresolved order intents.
- Both repository variables are global across branches. Scheduled workflows execute from the default branch; feature branches require explicit dispatch.
- Never migrate shared variables to a new schema before the corresponding code is merged to the default branch.

## Module boundaries

- `kraken_client.py`: authenticated CCXT client, strict GBP symbol validation, live minimum validation, deterministic order reconciliation, quote-cost submission, terminal-fill polling, and fee normalization.
- `crypto_dca.py`: rule/state validation, due and Dynamic DCA decisions, durable order-intent state machine, execution orchestration, and post-fill notification.
- `crypto_analysis.py`: public Kraken GBP OHLC analysis. Kraken returns at most 720 candles, so 15-minute analysis uses 3-, 5-, and 7-day periods.
- `portfolio_balance.py`: read-only Kraken balances, GBP cash, GBP prices, and buy history.
- `discord_bot.py`: natural-language interface, allowlist, config controls, five-minute Railway scheduler, status reader, and GitHub workflow dispatch.
- `gist_logger.py`: optional GBP Gist record.
- `portfolio_logger.py`: optional Ghostfolio adapter. Provider-specific conversion is isolated here and must never affect the Kraken GBP order.
- `.github/workflows/update_dca_config.yml`: one-field `DCA_TARGET_MAP` writer used by Discord.
- `.github/workflows/crypto_analysis.yml`: conflict-aware `TIME` writer.
- `.github/workflows/daily_dca.yml`: quick validation, due check, pending-intent recovery, and trader runner.

## Rules schema

`DCA_TARGET_MAP` contains desired behavior only:

```json
{
  "BTC_GBP": {
    "TIME": "02:45",
    "AMOUNT_GBP": 5,
    "BUY_ENABLED": false,
    "DYNAMIC_DCA": {
      "ENABLED": false,
      "THRESHOLD_PERCENT": -2,
      "REDUCED_MULTIPLIER": 0.5
    }
  }
}
```

Required invariants:

1. Keys match `^[A-Z0-9]+_GBP$`.
2. Entries are objects, not shorthand strings.
3. `TIME` is local 24-hour `HH:MM`; `BUY_ENABLED` is a boolean.
4. `AMOUNT_GBP` is numeric. Zero is allowed only while disabled as a migration placeholder.
5. Enabled targets require GBP 5 through GBP 1000. The Kraken client remains authoritative for stricter per-market minimums.
6. `LAST_BUY_DATE` and `PENDING_ORDER` never belong in this variable.

Discord may request changes only to `TIME`, `AMOUNT_GBP`, and `BUY_ENABLED`. It must dispatch `update_dca_config.yml`; it must not patch the shared map from Railway. The workflow fetches the latest map and applies one validated field. Analysis changes only `TIME` and must skip a field changed since its snapshot.

All Actions that can overlap repository-variable updates use concurrency group `dca-target-map-writers`, `queue: max`, and `cancel-in-progress: false`. Do not replace this with cancellation or direct read-modify-write calls from Railway.

## Execution-state schema

`DCA_EXECUTION_STATE` is valid as `{}` and is created or extended by the trader:

```json
{
  "BTC_GBP": {
    "LAST_BUY_DATE": "2026-08-04",
    "PENDING_ORDER": {
      "client_order_id": "dca-0123456789abcd",
      "trade_date": "2026-08-04",
      "amount_gbp": 5
    }
  }
}
```

- `LAST_BUY_DATE` is empty or strict `YYYY-MM-DD`.
- `PENDING_ORDER` is normally absent. When present, its client ID matches `^dca-[0-9a-f]{14}$`, its date is strict `YYYY-MM-DD`, and its GBP amount is valid.
- Only trading code writes this variable. Discord and workflows may read it for status, due checks, and recovery dispatch.
- Never clear an intent just to permit another attempt. First establish whether Kraken accepted the original request.

## Durable order state machine

For a new order:

1. Validate both JSON variables, due status, enabled state, same-day state, and Dynamic DCA output.
2. Re-fetch and compare the live rule fields that authorize the trade.
3. Derive the deterministic client order ID and persist `PENDING_ORDER` before Kraken can receive a create request.
4. Re-fetch the live rules after persistence and perform a final pre-submit comparison.
5. Reconcile open and closed orders for that client ID before creating anything.
6. Submit one precise GBP quote-cost market buy only if no match exists and the intent is new.
7. After a confirmed fill, atomically set `LAST_BUY_DATE` and remove `PENDING_ORDER` in one execution-state write.

For an existing intent:

- Enter reconcile-only mode regardless of normal time, enabled state, or whether the symbol remains in the rules map.
- Never send another create request from that intent.
- Poll a matching Kraken order to a terminal result.
- Clear the intent only after a known safe pre-submit failure or confirmed terminal no-fill result.
- If submission or lookup outcome is unknown, retain the intent lock and alert for later reconciliation or manual review.
- If a fill is confirmed but the completion write fails, retain the intent lock. Optional loggers run only after the state transition succeeds.

The GitHub quick check must validate both variables before checkout or credential loading. A pending intent must force recovery execution. Railway must also dispatch unresolved intents on its five-minute ticks, independent of the normal target window.

## Fee and trade-data semantics

Normalized Kraken results distinguish actual GBP debit from fee value:

```python
{
    "ts": unix_seconds,
    "amount_crypto": net_received_quantity,
    "cost_gbp": confirmed_order_cost,
    "gbp_fee_debit": fee_actually_charged_from_gbp,
    "fee_gbp": gbp_equivalent_of_all_fee_entries,
    "amount_gbp": confirmed_order_cost + fee_actually_charged_from_gbp,
    "gbp_price_per_unit": confirmed_order_cost / gross_filled_quantity,
    "effective_gbp_price_per_unit": actual_gbp_debit / net_received_quantity,
    "order_id": kraken_order_id,
}
```

If a fee is charged in the purchased asset, subtract it from received quantity. Its GBP equivalent contributes to `fee_gbp` for reporting but does not contribute to `gbp_fee_debit` or get added again to `amount_gbp`.

Logging is best-effort after a confirmed fill and completed execution-state transition. A Gist or Ghostfolio failure must not turn a completed order into a retry.

## Discord and scheduling safety

- Do not add a direct purchase command.
- Configuration writes require an allowlisted user and the exact `!dca ` prefix.
- Enabling requires the exact, unexpired second confirmation generated by the bot; if amount or time changes meanwhile, require a new confirmation.
- Pass that confirmed amount/time snapshot into the queued writer and compare it with the live rules again before applying `BUY_ENABLED=true`.
- Railway ticks every five minutes and dispatches enabled targets only in the same-local-day window from five minutes before through sixty minutes after `TIME`.
- A workflow dispatch acknowledgment is not proof that a trade completed. Refresh execution state and permit a guarded retry if completion is not recorded.
- GitHub fallback schedules run at 22:00 UTC and 16:55 UTC; the latter is the same-local-day catch-up for late targets.

## Deployment and migration

- Treat shared repository variables as production state even when working on a feature branch.
- Keep the Railway scheduler off and all targets disabled during migration.
- Merge code first. Then write disabled GBP rules to `DCA_TARGET_MAP`; migrate existing completion dates into `DCA_EXECUTION_STATE` and preserve any state already present. Initialize it to `{}` only when there is no unresolved order or same-local-day fill to suppress.
- Point Railway to `main`, redeploy, verify status, run the read-only portfolio job, and run a disabled trade workflow before enabling the scheduler.
- Enable one reviewed target only after its GBP amount and Kraken minimum are checked.
- Do not use a real order as a deployment smoke test.

## Secrets and permissions

- Do not print credentials, full environment dumps, authentication headers, or Discord tokens.
- Kraken keys need Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades, and Create & Modify Orders.
- Kraken keys must not have withdrawal permission.

## Validation

Use the repository virtual environment on Windows and force UTF-8 for notification output:

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m py_compile crypto_analysis.py crypto_dca.py discord_bot.py gist_logger.py kraken_client.py portfolio_balance.py portfolio_logger.py
```

Also validate workflow YAML, README Mermaid blocks, Docker build, and `git diff --check` when those areas change. Mock all authenticated order calls. Live checks must be read-only while buying is disabled.

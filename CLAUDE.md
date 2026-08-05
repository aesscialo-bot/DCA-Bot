# Repository guidance

This is a production Kraken GBP DCA bot. Read `README.md` before changing its schemas or release flow.

## Non-negotiable invariants

- Supported targets are exactly `BTC_GBP`, `ETH_GBP`, `SOL_GBP`, and `ADA_GBP`.
- `DCA_TARGET_MAP` contains only `REGIME_AMOUNTS_GBP` (`LOW` and `UP`) and `BUY_ENABLED` per target.
- `DCA_ANALYSIS_STATE` owns regimes and absolute execution windows.
- `DCA_EXECUTION_STATE` owns `LAST_BUY_DATE` and durable `PENDING_ORDER` data.
- Rules, analysis, and execution state must be validated through `dca_config.py`; do not duplicate a looser schema in a workflow or interface.
- Zero budgets are permitted only while disabled. Enabling requires both budgets to pass bot guardrails and a fresh Kraken market-minimum check.
- Budget writes are atomic and disabled-only. Enable writes are bound to the reviewed target hash, global four-asset rules hash, decision ID, and absence of pending intents.
- Python alone selects regime, amount tier, and execution time. Gemini is explanation-only.
- Use completed Kraken candles. Missing, insufficient, failed, stale, or missed analysis skips the purchase and alerts.
- The execution window is inclusive from five minutes before through 60 minutes after `EXECUTE_AT`.
- Preserve one buy per enabled asset per Asia/Bangkok calendar day.
- Persist a pending intent before Kraken submission, include `decision_id`, and reconcile it without creating a duplicate.
- Re-fetch live rules and analysis before intent creation and again immediately before Kraken submission.
- Request Kraken fees from the purchased base asset and never allow cost precision to raise the GBP debit above the selected budget.
- Gist and Ghostfolio are optional post-fill loggers and cannot influence spend or trade success.
- Never log credentials, tokens, or complete production JSON documents.
- Kraken keys must never have withdrawal permission.

## Cloud boundaries

- Railway runs `python3 -u discord_bot.py`, reads state, schedules absolute decisions, and dispatches workflows.
- GitHub Actions holds Kraken credentials and runs analysis, trading, configuration writes, and portfolio checks.
- Production follows `main`; releases go through a CI-gated pull request.
- `GH_PAT_FOR_VARS` is for repository variables. `GIST_TOKEN` is for Gist.
- Keep every target disabled during deployment verification. A verification run must never require a real order.

## Verification

Run before publishing:

```powershell
python -m compileall -q .
python -m unittest discover -s tests -v
docker build -t dca-bot-local .
```

CI must also parse every workflow file. Update tests whenever a state boundary, trend threshold, timing tie-break, scheduler rule, command phrase, or order-safety condition changes.

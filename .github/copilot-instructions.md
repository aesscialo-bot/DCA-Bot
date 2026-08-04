# Copilot instructions

This repository is a strict Kraken Spot GBP DCA system. Read `CLAUDE.md` and `README.md` before changing behavior.

## Non-negotiable invariants

- Store desired rules in `DCA_TARGET_MAP` as `COIN_GBP` entries with numeric `AMOUNT_GBP`, boolean `BUY_ENABLED`, and local `HH:MM` `TIME`.
- Send `AMOUNT_GBP` directly to Kraken as a GBP quote-cost market buy. Accept no alternate execution exchange or quote currency, and never infer a budget conversion.
- Keep `LAST_BUY_DATE` and `PENDING_ORDER` out of the rules map. They belong only in trader-owned `DCA_EXECUTION_STATE`.
- Persist a deterministic order intent before Kraken can receive a create request.
- An existing pending intent is reconciliation-only: search its open and closed Kraken orders, never create another order, and retain the lock when the outcome is unknown.
- Complete a confirmed fill by atomically setting `LAST_BUY_DATE` and removing `PENDING_ORDER`. Clear an intent separately only after a known safe no-fill outcome.
- Re-fetch and compare live rules before intent creation and immediately before a new submission.
- Keep `cost_gbp`, actual `gbp_fee_debit`, all-fee `fee_gbp` equivalent, actual total GBP debit, gross fill, and net received quantity distinct.
- A fee charged in the purchased asset reduces net quantity; its GBP equivalent is informational and is not an extra GBP debit.
- Do not add a direct purchase command. Enabling requires an allowlisted user, the exact `!dca ` prefix, and the exact second confirmation.
- Bind that confirmation to its amount/time snapshot and re-check the snapshot inside the queued writer before enabling.
- Keep optional logging failures non-blocking after a confirmed fill and completed state transition.

## Writers and scheduling

- Discord queues one-field rule edits through `.github/workflows/update_dca_config.yml`; Railway must not patch `DCA_TARGET_MAP` directly.
- The config workflow fetches the latest map and may change only `TIME`, `AMOUNT_GBP`, or `BUY_ENABLED`.
- Analysis may merge only `TIME` and must preserve a concurrent edit detected against its snapshot.
- Actions that may overlap variable writes use concurrency group `dca-target-map-writers`, `queue: max`, and `cancel-in-progress: false`.
- Railway ticks every five minutes and dispatches the trade workflow within the same-local-day `-5/+60` minute window. Pending intents are always eligible for recovery dispatch.
- The GitHub quick check validates both variables and runs the trader whenever any pending intent exists.

## Deployment model

- Railway hosts `discord_bot.py` and dispatches workflows through GitHub; Kraken credentials stay in GitHub Actions.
- GitHub Actions performs analysis, rule writes, Kraken execution, and Kraken portfolio reporting.
- Repository variables are global across branches; scheduled workflows use the default branch.
- Merge the code before migrating shared variables. Keep the scheduler off and every target disabled, then set disabled GBP rules and initialize execution state only after confirming there is no unresolved order.
- Do not mutate production trading configuration during a branch-only code change unless the migration is explicitly authorized.

## Testing

- Mock all authenticated Kraken order calls.
- Cover strict GBP parsing, direct GBP cost behavior, live minimums, durable-intent persistence, reconcile-only recovery, unknown outcomes, same-day completion, fee currency handling, serialized writes, five-minute scheduling, disabled targets, portfolio reporting, and optional logger failures.
- Run tests with UTF-8 enabled on Windows.
- Do not perform a real trade as a smoke test.

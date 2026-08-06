# Repository operating rules

This is the production Kraken GBP-funded USD-market DCA bot. Read
`00_START_HERE.md` and `README.md` before changing schemas, trading behavior,
workflows, or release configuration.

## Production invariants and current defaults

- Supported targets are exactly `BTC_USD`, `HYPE_USD`, and `SOL_USD`.
- `DCA_TARGET_MAP` contains only `REGIME_AMOUNTS_GBP` (`LOW` and `UP`) and
  `BUY_ENABLED` for each exact target.
- The current release gate is `DCA_START_DATE=2026-08-07`, interpreted as a
  strict `YYYY-MM-DD` date in `Asia/Bangkok`; all earlier trading fails closed.
- Downtrend and sideways select `LOW`; uptrend selects `UP`.
- The current approved baseline budgets are BTC £10/£20, HYPE £10/£15, and SOL
  £5/£15. Budget changes must use the guarded configuration flow.
- Each buy is two reconciled Kraken orders: sell the exact GBP budget on
  `GBP/USD` with `fciq`, then spend confirmed net USD on the target with `fcib`.
- One purchase per enabled asset per Bangkok calendar day is permitted.
- Stale, missing, insufficient, or inconsistent state always skips trading.
- A pending funding or crypto order must be reconciled before any new order.
- Kraken is the portfolio and execution source of truth.
- Gist and Ghostfolio are optional post-fill mirrors and never influence spend,
  success, scheduling, trend classification, or recovery.

## Ownership and security

- Railway continuously runs the Discord controller and scheduler.
- GitHub Actions holds Kraken credentials and performs trading and reporting.
- Kraken keys require query/order permissions and must not permit withdrawals.
- `GIST_TOKEN` is only for Gist; `GH_PAT_FOR_VARS` is only for repository state.
- Never log tokens, API responses containing credentials, complete production
  rules, complete analysis state, or complete execution state.
- Workflows load production JSON inside a step, mask every nonempty line, and
  pass it through `$GITHUB_ENV`; do not restore direct `${{ vars.DCA_* }}` JSON
  interpolation in a public workflow log path.

## Required verification

- Compile Python, run the complete unit suite, validate every workflow YAML,
  and build the Docker image before merging.
- Preserve deterministic regime/timing behavior and final live-state checks.
- Test both legs, partial/unknown responses, durable recovery, duplicate
  suppression, start-date boundaries, USD minimums, and GBP budget ceilings.
- Portfolio reports must include GBP and USD cash and value configured holdings
  in GBP using Kraken's live `GBP/USD` rate.
- User-facing post-fill wording must say `Saved on Kraken`; an absent optional
  Ghostfolio mirror must never be described as `Portfolio not saved`.

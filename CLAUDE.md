# Repository operating rules

This is the production Kraken GBP-market, GBP-budgeted DCA bot. Read
`00_START_HERE.md` and `README.md` before changing schemas, trading behavior,
workflows, or release configuration.

## Production invariants and current defaults

- Supported targets are exactly `BTC_GBP`, `ETH_GBP`, and `SOL_GBP`.
- `DCA_TARGET_MAP` contains only `REGIME_AMOUNTS_GBP` (`LOW` lower endpoint and
  compatibility-named `UP` upper endpoint) and `BUY_ENABLED` for each target.
- The current release gate is `DCA_START_DATE=2026-08-07`, interpreted as a
  strict `YYYY-MM-DD` date in `Asia/Bangkok`; all earlier trading fails closed.
- Counter-cyclical spend mapping is downtrend=`HIGH`, sideways=`MID`, and
  uptrend=`LOW`. `MID` is the half-up penny-rounded arithmetic midpoint.
- The current lower/higher endpoints are BTC £12.50/£25, ETH £12.50/£18.75,
  and SOL £12.50/£18.75, producing sideways amounts £18.75/£15.63/£15.63.
  Budget changes must use
  the guarded configuration flow and must keep the lower endpoint at or below
  the higher endpoint.
- BTC, ETH, and SOL are direct GBP buys on `BTC/GBP`, `ETH/GBP`, and `SOL/GBP`.
  No active target has a funding leg.
- One purchase per enabled asset per Bangkok calendar day is permitted.
- Stale, missing, insufficient, or inconsistent state always skips trading.
- A pending funding or crypto order must be reconciled before any new order.
- Kraken is the portfolio and execution source of truth.
- The private repository outbox and Ghostfolio are post-fill mirrors and never
  influence spend, success, scheduling, trend classification, or recovery.
- `DCA_RETIRED_TARGET_STATE` preserves the hash-bound final HYPE rule, analysis,
  and buy date. The fixed 7 August HYPE recovery assets remain historical and
  must not be converted into ETH fixtures.

## Ownership and security

- Railway continuously runs the Discord controller and scheduler.
- GitHub Actions holds Kraken credentials and performs trading and reporting.
- Kraken keys require query/order permissions and must not permit withdrawals.
- `DCA_OUTBOX_REPOSITORY_TOKEN` must be rotated after launch to Contents
  read/write on the dedicated private outbox repository only. The cutover's
  classic compatibility value is time-bounded. `GH_PAT_FOR_VARS` is only for
  repository state. The three outbox producers have no Gist fallback. `GIST_TOKEN` remains
  restricted to the separate market-history Gist during its transition.
- Never log tokens, API responses containing credentials, complete production
  rules, complete analysis state, or complete execution state.
- Workflows load production JSON inside a step, mask every nonempty line, and
  pass it through `$GITHUB_ENV`; do not restore direct `${{ vars.DCA_* }}` JSON
  interpolation in a public workflow log path.

## Required verification

- Compile Python, run the complete unit suite, validate every workflow YAML,
  and build the Docker image before merging.
- Preserve deterministic regime/timing behavior and final live-state checks.
- Test direct fills, partial/unknown responses, durable recovery, duplicate
  suppression, start-date boundaries, live minimums, and GBP budget ceilings.
  Preserve the fixed historical HYPE two-leg recovery tests unchanged.
- Portfolio reports must include GBP and USD cash and value configured holdings
  in GBP using Kraken's live `GBP/USD` rate.
- User-facing post-fill wording must say `Saved on Kraken`; an absent optional
  Ghostfolio mirror must never be described as `Portfolio not saved`.

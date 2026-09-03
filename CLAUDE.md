# Repository operating rules

This is the production Kraken GBP-market, GBP-budgeted DCA bot. Read
`00_START_HERE.md` and `README.md` before changing schemas, trading behavior,
workflows, or release configuration.

## Production invariants and current defaults

- Supported targets are exactly `BTC_GBP`, `ETH_GBP`, `SOL_GBP`, and `DOGE_GBP`.
- `DCA_TARGET_MAP` contains only `REGIME_AMOUNTS_GBP` (explicit `LOW`, `MID`,
  and compatibility-named `UP`) and `BUY_ENABLED` for each target.
- The current release gate is `DCA_START_DATE=2026-08-07`, interpreted as a
  strict `YYYY-MM-DD` date in `Asia/Bangkok`; all earlier trading fails closed.
- Counter-cyclical spend mapping is downtrend=`HIGH`, sideways=`MID`, and
  uptrend=`LOW`; all three amounts are explicit and satisfy `LOW <= MID <= UP`.
- Normal `UPTREND` requires the latest 3 completed daily closes above each
  candle's own SMA150. The first break returns `SIDEWAYS`. `DOWNTREND` requires
  the latest 3 closes below their SMA150 values and the latest EMA20 below EMA50.
  Weekly EMA and 20-day SMA150 slope remain informational. Preserve the
  170-daily/20-weekly data minimum.
- `DCA_UPTREND_OVERRIDE_STATE` is an optional, strict, versioned per-target
  emergency state. An active override has absolute precedence, is surfaced in
  analysis signals and Discord status, and forces effective `UPTREND` while
  `ACTIVE=true`. The analysis-driven automatic release path is gated on normal
  3-close confirmation and must persist the release before the matching
  analysis state. Gemini and routine Discord commands cannot write an override.
- Current LOW/MID/UP amounts are BTC £5/£10/£20 and ETH/SOL £5/£10/£15.
  DOGE begins disabled at £0/£0/£0 until the operator explicitly chooses budgets.
  Budget changes must use the guarded configuration flow and preserve ordering.
- BTC, ETH, SOL, and DOGE are direct GBP buys on `BTC/GBP`, `ETH/GBP`, `SOL/GBP`, and `DOGE/GBP`.
  No active target has a funding leg.
- One purchase per enabled asset per Bangkok calendar day is permitted.
- Stale, missing, insufficient, or inconsistent state always skips trading.
- Before intent creation and immediately before Kraken submission, re-read the
  live override document and require an exact match with the analysis decision.
  Preserve reconciliation-only recovery for an existing durable pending intent.
- A pending funding or crypto order must be reconciled before any new order.
- Kraken is the portfolio and execution source of truth.
- The private repository outbox and Ghostfolio are post-fill mirrors and never
  influence spend, success, scheduling, trend classification, or recovery.
- `DCA_RETIRED_TARGET_STATE` preserves the hash-bound final HYPE rule, analysis,
  and buy date. The fixed 7 August HYPE recovery assets remain historical and
  must not be converted into ETH fixtures.
- The HYPE-to-ETH migration carries the validated HYPE `LAST_BUY_DATE` to ETH
  only to preserve the once-per-Bangkok-day allocation guard. It never carries
  HYPE analysis into active ETH state.

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
- Treat every override activation as an auditable maintainer production-state
  change: canonical target, activation timestamp, and nonempty reason are
  required. Missing/blank means no override; malformed present state fails
  closed. Validator acceptance of an inactive entry is not proof that analysis
  wrote it; manual deactivation or removal is break-glass only.
- Workflows load production JSON inside a step, mask every nonempty line, and
  pass it through `$GITHUB_ENV`; do not restore direct `${{ vars.DCA_* }}` JSON
  interpolation in a public workflow log path.

## Required verification

- Compile Python, run the complete unit suite, validate every workflow YAML,
  and build the Docker image before merging.
- Preserve deterministic regime/timing behavior, override audit/release
  ordering, visible active-override warnings, and final live-state checks.
- Test direct fills, partial/unknown responses, durable recovery, duplicate
  suppression, start-date boundaries, live minimums, and GBP budget ceilings.
  Preserve the fixed historical HYPE two-leg recovery tests unchanged.
- Portfolio reports must include GBP and USD cash and value configured holdings
  in GBP using Kraken's live `GBP/USD` rate.
- User-facing post-fill wording must say `Saved on Kraken`; an absent optional
  Ghostfolio mirror must never be described as `Portfolio not saved`.

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
- Current approved LOW/MID/UP amounts are BTC £5/£10/£20 and ETH/SOL/DOGE
  £5/£10/£15. DOGE's approved configuration is persisted; all four flags remain
  disabled. All-enabled aggregate LOW/MID/UP exposure is £20/£40/£65.
  Budget changes must use the guarded configuration flow and preserve ordering.
- This review-and-polish release is deployed paused: every `BUY_ENABLED=false`,
  GitHub and Railway both `DCA_TRADING_MODE=shadow`, and Railway
  `DCA_CRON_ENABLED=false`. Deployment/review success is not live-activation
  approval. No target-membership migration is required; retain existing archives.
- BTC, ETH, SOL, and DOGE are direct GBP buys on `BTC/GBP`, `ETH/GBP`, `SOL/GBP`, and `DOGE/GBP`.
  No active target has a funding leg.
- One purchase per enabled asset per Bangkok calendar day is permitted.
- Stale, missing, insufficient, or inconsistent state always skips trading.
- Timing evidence separates verified `COVERAGE_THROUGH` (alias `THROUGH`) from
  `LAST_REAL_CANDLE_AT` (last traded candle start). At analysis creation, preserve
  the 45-minute verified-coverage freshness gate. Carry last-close, zero-volume
  candles only for proven no-trade intervals after the first real candle through
  the verified cutoff; never conceal partial ingestion or invent leading data.
  Version-2 history evidence and timing policy v4 invalidate older decisions.
- A fresh executable direct-GBP Kraken quote is required for order sizing and
  market minimums; carried historical prices never authorize a new order.
- Exact enable confirmation expires after five minutes and binds reviewed
  budgets and all four rules. Every enable invalidates the selected target's old
  analysis before the rules write under both writer locks, verifies readback,
  and waits for new successful analysis. Missing/malformed/obsolete analysis
  blocks enable until repaired; never clear execution state or unrelated decisions.
- Bulk `!dca enable all` reviews every target's three budgets and aggregate
  maximum exposure, including mixed current flags. Exact `!dca confirm enable all`
  is required within five minutes. Validate all four before any enabling write;
  one failed target fails the whole operation. After `APPLIED`, require successful
  fresh `!dca analyze all`; no old decision may revive through bulk enable.
- `!dca disable all` queues a single atomic all-four disable without confirmation.
  It never changes budgets, modes, scheduling, buy dates, or recovery evidence.
  Queued is not applied, and accepted Kraken orders still require reconciliation.
- The newest main configuration request supersedes any older queued enable,
  including when the newer request is a no-op disable or dry run. Verify complete
  GitHub run evidence under the writer lock and fail enable closed if unreadable.
  Configuration attempt reruns are refused before writes; require a fresh request.
  Never replay historical pre-guard configuration runs against production.
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
- A missing/empty Discord allowlist denies private reads, report dispatches,
  and configuration operations. Suppress mentions and escape untrusted text.
- Queued configuration is not applied. Completion receipts require exact rules
  readback, fixed sanitized wording, and the expected GitHub run URL. A failed
  receipt is visible but never automatically retries the configuration write.
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
- Require independent trading-safety and Discord-usability review, a full-day
  isolated shadow simulation with zero order submissions, and deployed-SHA,
  worker/log/Discord checks. Verify fresh all-four analysis and the next scheduled
  GitHub cycle; report this release as deployed and paused, never buying live.
- Preserve deterministic regime/timing behavior, override audit/release
  ordering, visible active-override warnings, and final live-state checks.
- Test direct fills, partial/unknown responses, durable recovery, duplicate
  suppression, start-date boundaries, live minimums, and GBP budget ceilings.
  Preserve the fixed historical HYPE two-leg recovery tests unchanged.
- Portfolio reports must include GBP and USD cash and value configured holdings
  in GBP using Kraken's live `GBP/USD` rate.
- User-facing post-fill wording must say `Saved on Kraken`; an absent optional
  Ghostfolio mirror must never be described as `Portfolio not saved`.

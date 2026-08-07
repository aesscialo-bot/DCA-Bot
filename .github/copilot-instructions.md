# Copilot instructions

This repository is the production Kraken mixed-market, GBP-budgeted DCA bot. Read
`00_START_HERE.md`, `README.md`, and `CLAUDE.md` before changing behavior,
schemas, workflows, or deployment configuration.

## Current production contract

- Supported targets are exactly `BTC_GBP`, `HYPE_USD`, and `SOL_GBP` unless a
  deliberately staged pair-membership migration changes the full system.
- `DCA_TARGET_MAP` contains every canonical target with only
  `REGIME_AMOUNTS_GBP` (`LOW` lower endpoint and compatibility-named `UP` upper
  endpoint) and boolean `BUY_ENABLED`.
- Budgets remain GBP-denominated and execute in each configured quote currency.
- BTC and SOL use direct Kraken GBP markets; HYPE uses `HYPE/USD` with the
  explicit GBP/USD funding leg. Never use implicit conversion, THB, Bitkub,
  or the legacy `AMOUNT_GBP` / `TIME` schema.
- For HYPE only, sell the exact GBP budget on Kraken `GBP/USD` with `fciq`, wait
  for confirmed net USD, and spend that USD on `HYPE/USD` with `fcib`.
- Counter-cyclical spend mapping is `DOWNTREND`→`HIGH`, `SIDEWAYS`→`MID`, and
  `UPTREND`→`LOW`. `MID` is `(LOW + UP) / 2`, rounded to the nearest penny with
  `ROUND_HALF_UP`; `HIGH` reads the stored `UP` endpoint. Never infer spend from
  the similarity between the `UP` endpoint name and `UPTREND`.
- Deterministic Python chooses regime, tier, and execution time from completed
  Kraken candles. Gemini Flash-Lite may explain but never choose or override.
- `DCA_START_DATE` is a strict Asia/Bangkok release gate. One purchase per
  enabled target per Bangkok calendar day is permitted.

## State ownership and order safety

- Analysis owns `DCA_ANALYSIS_STATE`, including `STATUS`, `REGIME`,
  `AMOUNT_TIER`, `EXECUTE_AT`, `VALID_UNTIL`, `DECISION_ID`, `RULES_HASH`,
  signals, and timing metrics.
- READY `AMOUNT_TIER` is exactly `LOW`, `MID`, or `HIGH` and must match the
  counter-cyclical regime mapping. Policy-version changes invalidate old
  decisions and require fresh analysis.
- The trader owns `DCA_EXECUTION_STATE`, including `LAST_BUY_DATE` and durable
  `PENDING_ORDER` state for both funding and crypto legs.
- Persist a deterministic two-leg intent before Kraken can receive a create
  request. Distinct deterministic client IDs are mandatory.
- An existing pending intent is reconciliation-only. Search open and closed
  Kraken orders, never create a replacement leg, and retain the lock while the
  outcome is unknown.
- Complete a confirmed flow by atomically setting `LAST_BUY_DATE` and removing
  `PENDING_ORDER`. Clear an intent separately only after a known safe no-fill
  outcome.
- Re-fetch and compare live rules and decision state before intent creation and
  immediately before Kraken submission.
- Missing, stale, insufficient, mismatched, or failed analysis always skips the
  purchase. Never reuse an old decision.
- Keep requested GBP, funding fees, confirmed USD proceeds, crypto cost, crypto
  fees, gross quantity, and net received quantity distinct.

## Configuration and scheduling

- Exact Discord controls queue serialized writes through
  `.github/workflows/update_dca_config.yml`; Railway never patches repository
  variables directly.
- Budget edits atomically replace the lower `LOW` and upper `UP` endpoints,
  require `LOW <= UP`, use no more than two decimal places, and require the
  target to be disabled.
- Enabling requires a fresh matching decision, zero pending intents, a live
  Kraken minimum check, an allowlisted user, the exact `!dca ` prefix, and the
  exact second confirmation bound to the global rules snapshot.
- Analysis writes the complete `DCA_ANALYSIS_STATE`; it never merges timing
  fields into `DCA_TARGET_MAP`.
- Rule writers use `dca-rule-writers`; analysis writers use
  `dca-analysis-state-writers`; the trader uses its own serialized execution
  group. Preserve `queue: max` and `cancel-in-progress: false`.
- The analysis workflow uses 3/5/7/14/30/45/60-day Bangkok windows and the
  canonical BTC/GBP, HYPE/USD, and SOL/GBP markets.
- Primary analysis is scheduled for 04:07 Asia/Bangkok with an idempotent
  04:37 recovery run.
  Railway refreshes state and checks due decisions every five minutes.
- Execution is permitted only inside the decision's absolute `-5/+60` minute
  window and after `DCA_START_DATE`, with no more than one purchase per target
  per Bangkok calendar day. Pending intents remain eligible for recovery.
- `DCA_CRON_ENABLED` belongs to the Railway environment, not GitHub repository
  variables. `TIMEZONE=Asia/Bangkok` must match in both places.

## Pair-membership changes

- Disabling is the normal operational meaning of removing a pair. Do not delete
  a required JSON key.
- Adding or permanently removing a pair is a tested code and state migration,
  not a JSON-only edit.
- Update `dca_config.py`, Discord aliases and fixed-target copy/counts,
  `crypto_analysis.py`, `kraken_order_audit.py`, workflow inputs, documentation,
  and the complete relevant test matrix.
- Keep all targets disabled and Railway scheduling off during the structural
  migration. Audit Kraken, confirm zero pending intents, and migrate complete
  rules, analysis, and execution state without losing legitimate buy dates or
  recovery records. Run portfolio and analysis checks, then enable deliberately.
- A new market needs Kraken `BASE/USD`, the `GBP/USD` funding path, a valid live
  minimum, 170 completed daily candles, 20 weekly candles, and seven complete
  days of 15-minute candles.

## Deployment and security

- Railway continuously runs `discord_bot.py` and dispatches GitHub workflows.
- GitHub Actions performs public-market analysis, repository-state writes,
  authenticated Kraken portfolio checks, and order execution.
- Repository variables are global across branches; scheduled workflows use the
  default branch. Do not mutate production state during a branch-only code
  change unless the migration is explicitly authorized.
- Kraken keys require query/order permissions and must never allow withdrawals.
- `GIST_TOKEN` is only for Gist; Actions `GH_PAT_FOR_VARS` is only for repository
  state; Railway `GH_PAT` is the Discord controller's workflow/variable token.
- Never log tokens, full production JSON, or unredacted authenticated payloads.
- Optional Gist, Ghostfolio, and Gemini failures must not change authoritative
  Kraken execution, spend, state transition, or deterministic analysis.

## Testing

- Mock every authenticated Kraken order call. Never perform a real trade as a
  smoke test.
- Compile Python, run the complete unit suite, validate all workflow YAML, and
  build the Railway Docker image before merging.
- Cover exact target schemas, atomic budgets, start-date boundaries, trend and
  timing rules, live minimums, both order legs, partial/unknown responses,
  reconcile-only recovery, duplicate suppression, final live-state checks,
  scheduler windows, portfolio reporting, and optional logger failures.
- Cover all three exact regime/tier mappings, ordered endpoints, equal
  endpoints, half-penny midpoint rounding, and rejection of obsolete analysis
  state versions/tier pairs.
- Run tests with UTF-8 enabled on Windows.

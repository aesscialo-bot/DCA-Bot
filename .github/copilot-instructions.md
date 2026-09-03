# Copilot instructions

This repository is the production Kraken GBP-market, GBP-budgeted DCA bot. Read
`00_START_HERE.md`, `README.md`, and `CLAUDE.md` before changing behavior,
schemas, workflows, or deployment configuration.

## Current production contract

- Supported targets are exactly `BTC_GBP`, `ETH_GBP`, `SOL_GBP`, and `DOGE_GBP` unless a
  deliberately staged pair-membership migration changes the full system.
- `DCA_TARGET_MAP` contains every canonical target with only
  `REGIME_AMOUNTS_GBP` (explicit `LOW`, `MID`, and compatibility-named `UP`)
  and boolean `BUY_ENABLED`.
- Budgets remain GBP-denominated. BTC, ETH, SOL, and DOGE execute directly on Kraken
  `BTC/GBP`, `ETH/GBP`, `SOL/GBP`, and `DOGE/GBP`; no active target has a funding leg.
  Never use implicit conversion, THB, Bitkub, or the legacy `AMOUNT_GBP` /
  `TIME` schema.
- Counter-cyclical spend mapping is `DOWNTREND`→`HIGH`, `SIDEWAYS`→`MID`, and
  `UPTREND`→`LOW`. All three amounts are explicit; `HIGH` reads the stored `UP`
  field. Never infer spend from
  the similarity between the `UP` endpoint name and `UPTREND`.
- Normal `UPTREND` requires the latest 3 completed daily closes above each
  candle's own SMA150. The first break returns `SIDEWAYS`. `DOWNTREND` requires
  the latest 3 closes below their SMA150 values plus latest EMA20 below EMA50.
  Weekly EMA and 20-day SMA150 slope are informational; retain the
  170-daily/20-weekly minimum.
- Deterministic Python chooses regime, tier, and execution time from completed
  Kraken candles. Gemini Flash-Lite may explain but never choose a regime or
  create, change, or release an emergency override.
- `DCA_START_DATE` is a strict Asia/Bangkok release gate. One purchase per
  enabled target per Bangkok calendar day is permitted.

## State ownership and order safety

- Analysis owns `DCA_ANALYSIS_STATE`, including `STATUS`, `REGIME`,
  `AMOUNT_TIER`, `EXECUTE_AT`, `VALID_UNTIL`, `DECISION_ID`, `RULES_HASH`,
  signals, and timing metrics.
- `DCA_UPTREND_OVERRIDE_STATE` is optional strict versioned maintainer state.
  Missing/blank means no override; malformed present state fails closed. An
  active per-target entry has absolute precedence and forces effective
  `UPTREND`, even over a normal `DOWNTREND`, while analysis signals retain the
  normal result, confirmation progress, reason, and activation/release audit.
  Analysis automatically releases it only on natural 3-close confirmation and
  must persist that release before the matching `DCA_ANALYSIS_STATE`. Routine
  Discord controls and Gemini cannot write this state.
- READY `AMOUNT_TIER` is exactly `LOW`, `MID`, or `HIGH` and must match the
  counter-cyclical regime mapping. Policy-version changes invalidate old
  decisions and require fresh analysis.
- The trader owns `DCA_EXECUTION_STATE`, including `LAST_BUY_DATE` and durable
  `PENDING_ORDER` state. The schema retains distinct buy/funding client IDs for
  compatibility, but active direct-GBP routes submit only the buy ID.
- Persist the deterministic intent before Kraken can receive a create request.
  Never pass the reserved funding ID to a direct-GBP connector call.
- An existing pending intent is reconciliation-only. Search open and closed
  Kraken orders, never create a replacement leg, and retain the lock while the
  outcome is unknown.
- Complete a confirmed flow by atomically setting `LAST_BUY_DATE` and removing
  `PENDING_ORDER`. Clear an intent separately only after a known safe no-fill
  outcome.
- Re-fetch and compare live rules and decision state before intent creation and
  immediately before Kraken submission.
- Re-fetch the live optional uptrend-override document as the final validation
  at both boundaries and require an exact match with the analysis decision.
  Do not apply this new-order gate to reconciliation of a durable pending intent.
- Missing, stale, insufficient, mismatched, or failed analysis always skips the
  purchase. Never reuse an old decision.
- Keep requested GBP, quote-currency cost, fees, gross quantity, and net
  received quantity distinct. Preserve the richer two-leg fields on historical
  HYPE recovery records.

## Configuration and scheduling

- Exact Discord controls queue serialized writes through
  `.github/workflows/update_dca_config.yml`; Railway never patches repository
  variables directly.
- Budget edits atomically replace `LOW`, `MID`, and `UP`, require
  `LOW <= MID <= UP`, use no more than two decimal places, and require the
  target to be disabled.
- Enabling requires a fresh matching decision, zero pending intents, a live
  Kraken minimum check, an allowlisted user, the exact `!dca ` prefix, and the
  exact second confirmation bound to the global rules snapshot.
- Analysis writes the complete `DCA_ANALYSIS_STATE`; it never merges timing
  fields into `DCA_TARGET_MAP`.
- Discord status must visibly label each active emergency override and show its
  normal rule result, confirmation count, activation time, and audit reason.
- Rule writers use `dca-rule-writers`; analysis writers use
  `dca-analysis-state-writers`; the trader uses its own serialized execution
  group. Preserve `queue: max` and `cancel-in-progress: false`.
- The analysis workflow uses 3/5/7/14/30/45/60-day Bangkok windows and the
  canonical BTC/GBP, ETH/GBP, SOL/GBP, and DOGE/GBP markets.
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
- The HYPE-to-ETH cutover uses `migrate_hype_to_eth.yml`; retain its hash-bound
  rule/analysis/execution archive in `DCA_RETIRED_TARGET_STATE`. Bootstrap
  `ETH_GBP` history before fresh all-target analysis. The fixed 7 August HYPE
  recovery workflow, evidence, Ghostfolio provenance logic, and historical
  tests do not become ETH assets or active-target fixtures.
- Carry HYPE's validated `LAST_BUY_DATE` to ETH solely to preserve the
  once-per-Bangkok-day allocation guard; never carry HYPE analysis into ETH.
- A new market needs its exact Kraken spot market, a valid live GBP-equivalent
  minimum, 170 completed daily candles, 20 weekly candles, and seven complete
  days of 15-minute candles. Any non-GBP route additionally needs an explicit,
  reconciled funding path and its full route-specific test matrix.

## Deployment and security

- Railway continuously runs `discord_bot.py` and dispatches GitHub workflows.
- GitHub Actions performs public-market analysis, repository-state writes,
  authenticated Kraken portfolio checks, and order execution.
- Repository variables are global across branches; scheduled workflows use the
  default branch. Do not mutate production state during a branch-only code
  change unless the migration is explicitly authorized.
- Override activation is a deliberate maintainer repository-variable edit and
  requires the canonical target, canonical UTC `ACTIVATED_AT`, null
  `RELEASED_AT`, and a nonempty `REASON`. The analysis-driven automatic release
  is gated on confirmation. Validator acceptance of an inactive entry is not
  proof that analysis wrote it; manual deactivation or removal is break-glass
  only.
- Kraken keys require query/order permissions and must never allow withdrawals.
- `DCA_OUTBOX_REPOSITORY_TOKEN` must be rotated after launch to Contents
  read/write on the dedicated private outbox repository only. The cutover's
  classic compatibility value is time-bounded; Actions `GH_PAT_FOR_VARS` is only for
  repository state; Railway `GH_PAT` is the Discord controller's workflow/variable
  token. The event, holdings, and audit producers have no Gist fallback;
  `GIST_TOKEN` remains restricted to the separate market-history Gist.
- Never log tokens, full production JSON, or unredacted authenticated payloads.
- Optional outbox, Ghostfolio, and Gemini failures must not change authoritative
  Kraken execution, spend, state transition, or deterministic analysis.

## Testing

- Mock every authenticated Kraken order call. Never perform a real trade as a
  smoke test.
- Compile Python, run the complete unit suite, validate all workflow YAML, and
  build the Railway Docker image before merging.
- Cover exact target schemas, atomic budgets, start-date boundaries, trend and
  timing rules, live minimums, direct-order partial/unknown responses,
  reconcile-only recovery, duplicate suppression, final live-state checks,
  scheduler windows, portfolio reporting, and optional logger failures. Keep
  both legs covered by the fixed historical HYPE incident tests.
- Cover all three exact regime/tier mappings, ordered amounts, legacy midpoint
  normalization, and rejection of obsolete analysis
  state versions/tier pairs.
- Cover 2-versus-3 close uptrend boundaries, responsive downtrend confirmation,
  override precedence over sideways/downtrend, automatic release ordering,
  malformed override rejection, and visible active-override status labelling.
- Run tests with UTF-8 enabled on Windows.

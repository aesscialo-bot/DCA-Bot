# Start Here: DCA Bot Operating and Configuration Guide

This is the day-to-day guide for the production Kraken DCA bot. Use it to
check the bot, change budgets, stop or restart a pair, find the live services,
and understand when a code change is required.

The production source is [`main`](https://github.com/aesscialo-bot/DCA-Bot/tree/main).
Kraken is the source of truth for balances, holdings, fees, and orders.

## Quick links

| What you need | Link |
| --- | --- |
| Control and status commands | [DCA Bot Discord channel](https://discord.com/channels/1533662042366415040/1533662042916126824) |
| Cloud worker, logs, and deployment | [Railway `discord-bot` service](https://railway.com/project/a7bf235c-d1c4-4a3e-9890-738d36e2fb3d/service/d1e2d272-3803-481c-91b3-8cb1f0024e0f?environmentId=cf016489-a464-4104-8ebd-67e587724630) |
| Railway runtime variables | [Railway service variables](https://railway.com/project/a7bf235c-d1c4-4a3e-9890-738d36e2fb3d/service/d1e2d272-3803-481c-91b3-8cb1f0024e0f/variables?environmentId=cf016489-a464-4104-8ebd-67e587724630) |
| User-owned rules JSON | [GitHub Actions variables](https://github.com/aesscialo-bot/DCA-Bot/settings/variables/actions) |
| API keys and tokens | [GitHub Actions secrets](https://github.com/aesscialo-bot/DCA-Bot/settings/secrets/actions) |
| Daily analysis runs | [Crypto Analysis workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/crypto_analysis.yml) |
| Purchase workflow runs | [Daily Crypto DCA workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/daily_dca.yml) |
| Configuration changes | [Update DCA Configuration workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/update_dca_config.yml) |
| Kraken holdings report | [Portfolio Balance Check workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/portfolio_check.yml) |
| Code validation and deployment gate | [CI workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/ci.yml) |

## Recovery posture

The bot is intentionally contained while this recovery is validated:

- Railway scheduling is paused with `DCA_CRON_ENABLED=false`.
- `DCA_TRADING_MODE=shadow` blocks every new Kraken order.
- All pairs remain analysis-enabled, but the all-three Kraken history gate must
  pass before even the SOL canary can submit an order.
- The 7 August missed purchase will not be replayed.
- Rollback at every rollout stage is `DCA_TRADING_MODE=shadow`.

Do not resume Railway or choose `canary` until the history manifest shows three
verified 60-day decisions and one complete scheduled shadow cycle has produced
zero Kraken `AddOrder` calls.

## Production baseline

The configured target set is exactly:

| Pair | `UPTREND` lower | `SIDEWAYS` | `DOWNTREND` higher | Intended state |
| --- | ---: | ---: | ---: | --- |
| `BTC/GBP` | £5 | £10 | £20 | Enabled |
| `ETH/GBP` | £5 | £10 | £15 | Enabled |
| `SOL/GBP` | £5 | £10 | £15 | Enabled |

- The bot deliberately buys more in a `DOWNTREND`, the explicit middle amount in a
  `SIDEWAYS` market, and less in an `UPTREND`.
- Aggregate daily exposure is £50 / £30 / £15 when every pair is
  downtrend / sideways / uptrend respectively.
- Each enabled pair can buy at most once per Asia/Bangkok calendar day.
- The strict trading start gate is `DCA_START_DATE=2026-08-07` in
  `Asia/Bangkok`. Earlier orders are blocked.

This table is the approved baseline, not a substitute for checking live state.
Use `show status` and `!dca health` in Discord whenever you need the current
rules, decisions, pending state, and scheduler posture.

## What happens each day

1. GitHub Actions runs at 04:07 Asia/Bangkok, with an idempotent 04:37 recovery.
   Railway checks for a missing run after 04:20.
   From midnight until 04:20, a healthy prior-day state is an expected waiting
   posture: no old decision can trade, pending recovery remains active, and no
   stale-date incident is sent unless the state is otherwise unhealthy.
2. Deterministic Python classifies each pair as `UPTREND`, `DOWNTREND`, or
   `SIDEWAYS`. Uptrend requires the latest 3 consecutive completed daily closes
   above each candle's own SMA150. The first break returns sideways. Downtrend
   requires 3 completed closes below their own SMA150 values and a bearish latest
   EMA20/EMA50. Weekly EMA and SMA150 slope remain informational.
3. Python selects the higher / explicit middle / lower GBP spend for downtrend /
   sideways / uptrend respectively, plus the best 15-minute execution time from
   deterministic 3-, 5-, 7-, 14-, 30-, 45-, and 60-day timing windows on
   BTC/GBP, ETH/GBP, and SOL/GBP.
4. The workflow writes a fresh `DCA_ANALYSIS_STATE` and posts a readable summary
   to Discord.
5. Railway checks the absolute execution times every five minutes and dispatches
   the trader when a pair is due.
6. The trader rechecks the live rule, decision, uptrend-override document, date,
   window, Kraken minimum, pending-order state, and once-per-day guard. Override
   state must match the analysis before intent creation and immediately before
   Kraken submission.
7. BTC, ETH, and SOL spend GBP directly on `BTC/GBP`, `ETH/GBP`, and `SOL/GBP`.
   No active target has a funding leg.
8. Kraken remains the authoritative record and Discord receives the result.

Gemini Flash-Lite explains the completed Python decision. It cannot select or
change the regime, amount, pair, or execution time. If Gemini is unavailable,
the deterministic decision remains valid and only the optional explanation is
missing.

Missing, stale, insufficient, or failed analysis creates an `ERROR` decision,
alerts Discord, and skips that purchase. The bot never reuses an old decision.
The Daily DCA fallback also runs at minutes 02, 17, 32, and 47, but every trigger
passes through the same durable intent and once-per-day lock.

Status reports the analysis date, decision ID, selected optimal time, effective
catch-up time, execution status, workflow ref, pending Kraken intent, Portfolio
Compass delivery, and local Ghostfolio receipt completion. It prominently labels
any active emergency uptrend override with the normal rule result, confirmation
progress, activation time, and reason. A stale decision is never shown as
`READY` or `Next`.

## Everyday Discord controls

Commands are exact safety controls. Use the allowlisted Discord account and the
exact lowercase `!dca ` prefix and spacing.

### Check the bot

```text
show status
!dca status
!dca health
show portfolio
!dca portfolio
help
```

### Chat naturally with the bot

When `GEMINI_API_KEY` is configured on Railway, ordinary messages are routed by
Gemini Flash-Lite to an approved read-only status, health, portfolio, help, or
educational response. Replies use short reviewed explanations with relevant
emoji. Gemini-supplied prose and parameters are never posted or executed.

Natural language cannot change a budget, start analysis, enable or disable a
pair, confirm a change, or place a Kraken order. Those operations still require
the exact allowlisted `!dca` commands shown by `help`. If Gemini is unavailable,
the scheduler and exact commands continue normally and common read-only requests
use a deterministic fallback.

Before the configured start day or before its first 04:07 analysis, a pair may
show an `ERROR` placeholder while overall health correctly reports `ARMED`.
After a successful daily analysis, enabled pairs should show fresh `READY`
decisions, regimes, and execution times.

### Change all three budgets for an existing pair

Example: set BTC to £5 in uptrend, £10 sideways, and £20 in downtrend.

```text
!dca disable BTC
```

Wait for the first **Update DCA Configuration** run to succeed, then send:

```text
!dca set BTC amounts to 5 low, 10 sideways, and 20 high
```

Wait for the second **Update DCA Configuration** run to succeed. Only then send:

```text
!dca analyze BTC
```

Wait for the **Crypto Analysis** run to succeed and for Discord to show a fresh
`READY` result. Then send:

```text
!dca enable BTC
```

Review the budgets, latest regime, effective amount, next execution time, age,
and maximum aggregate daily exposure. Within five minutes, copy the exact
confirmation returned by the bot, for example:

```text
!dca confirm enable BTC_GBP
```

Wait for the final **Update DCA Configuration** run to succeed. Railway can take
up to five minutes to refresh the new schedule. Finish with:

```text
show status
!dca health
```

Replace `BTC` with `ETH` or `SOL` as needed. Enter numbers without a `£` sign
and with no more than two decimal places. Amounts must satisfy
`low <= sideways <= high`. All three must be between £5 and £1,000 and at or
above Kraken's current market minimum before enabling. Zero is permitted only
as a disabled placeholder. The old two-amount command remains a rollout
compatibility alias that derives its midpoint; use the explicit form above.

### Stop buying a pair

```text
!dca disable ETH
```

Disabling is the safe operational meaning of removing a pair. It prevents a new
order while allowing any order Kraken already accepted to be reconciled. The
pair remains visible and can still be analyzed. Do not delete its JSON key.

### Analyze now

```text
!dca analyze BTC
!dca analyze all
```

Manual analysis refreshes decisions. It does not bypass the start date,
execution window, minimum-order check, or once-per-day restriction.

### Emergency uptrend override (maintainers only)

`DCA_UPTREND_OVERRIDE_STATE` is an optional repository variable. Missing or blank
means no override. An active per-target entry forces the effective regime to
`UPTREND` even if the normal classifier returns `SIDEWAYS` or `DOWNTREND`, so it
is an exceptional production-state change rather than a routine Discord control.

The strict version-1 document contains canonical target keys and, for each
included target, only `ACTIVE`, `ACTIVATED_AT`, `RELEASED_AT`, and `REASON`.
Activation requires a deliberate maintainer edit with `ACTIVE=true`, a canonical
UTC activation timestamp, `RELEASED_AT=null`, and a nonempty audit reason. A
present malformed document fails analysis closed. Never put override fields in
`DCA_TARGET_MAP`, and never activate one through natural-language chat.

The analysis decision records the normal regime, the effective regime, the
trailing confirmation count out of 3, and the active/applied override audit
fields. `show status` displays a prominent warning for the affected target.
During normal operation, leave an active entry unchanged. The analysis workflow
automatically persists an inactive release immediately before the matching
analysis state when the latest 3 completed daily closes confirm `UPTREND`.
Repository-variable maintainers can technically deactivate or remove an
override; treat that as a break-glass production change, and do not treat an
inactive record alone as evidence of natural confirmation.

## Rules JSON and state ownership

The user-owned repository variable is `DCA_TARGET_MAP`. Its approved shape is:

```json
{
  "BTC_GBP": {
    "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 20},
    "BUY_ENABLED": true
  },
  "ETH_GBP": {
    "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 15},
    "BUY_ENABLED": true
  },
  "SOL_GBP": {
    "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 15},
    "BUY_ENABLED": true
  }
}
```

`LOW`, `MID`, and compatibility-named `UP` store the uptrend, sideways, and
downtrend amounts. `UP` does **not** mean the amount used in an uptrend. Analysis
records tiers `LOW`, `MID`, or `HIGH` according to the counter-cyclical policy.

Use Discord for routine changes. It validates and serializes the write, checks
fresh analysis before enabling, and checks Kraken's current market minimum.

If Discord is unavailable, the
[Update DCA Configuration workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/update_dca_config.yml)
can safely perform these limited operations:

- Disable: `action=set_enabled`, canonical `symbol`, `enabled_json=false`.
- Change budgets while disabled: `action=set_amounts`, canonical `symbol`, and
  numeric `low_amount_gbp_json`, `mid_amount_gbp_json`, and
  `up_amount_gbp_json` values.
- Validate without writing: `action=dry_run`, canonical `symbol`, all three
  numeric amount inputs, and a target that is already disabled.

The workflow input `up_amount_gbp_json` is a compatibility name for the
upper/higher endpoint.

Do not manually use `set_enabled=true`; safe enabling binds to fresh decision
and rules fingerprints that Discord supplies during exact confirmation.

Do not manually edit:

- `DCA_ANALYSIS_STATE`: owned by the analysis workflow.
- `DCA_EXECUTION_STATE`: owned by the trader; contains pending-order recovery
  and the durable Portfolio Compass ledger-delivery queue.
- API keys or tokens in JSON, Discord, source files, or public logs.

Changing any regime amount invalidates that target's old decision fingerprint.
Enabling or disabling changes the globally reviewed rules state.
Run fresh analysis after a budget change before trying to re-enable or trade.

## Adding or permanently removing a pair

Pair membership is a maintainer/Codex code-and-state migration, not a beginner
JSON setting. The current schema requires exactly `BTC_GBP`, `ETH_GBP`, and
`SOL_GBP`; an extra or missing key fails closed.

For a permanent pair change:

1. Disable every target through Discord.
2. Set `DCA_CRON_ENABLED=false` in Railway service variables.
3. Run a read-only Kraken order audit and confirm there are no unresolved or
   pending intents. Never discard an order or pending state to make this pass.
4. Confirm the exact new Kraken spot market exists and satisfies the current
   GBP-equivalent market minimum. A non-GBP route also requires a reviewed,
   explicit funding path; `ETH/GBP` is direct and has no funding leg.
5. Confirm the market has at least 170 completed daily candles, 20 completed
   weekly candles, and seven complete days of 15-minute data.
6. Have a maintainer prepare a staged compatibility/migration pull request that
   changes the canonical list, validation, workflow, audit, documentation, and
   test matrix. The release must also provide a guarded one-time migration for
   the complete rules, analysis, and execution state.
7. Preserve every legitimate `LAST_BUY_DATE` and pending recovery record during
   execution-state migration. A removed target cannot be dropped while it has a
   pending intent.
8. Merge only after full CI and Docker validation pass. If the release uses a
   single cutover instead of staged compatibility, expect a deliberate temporary
   `NOT READY` fail-closed window while the state migration runs.
9. For the HYPE-to-ETH cutover, run the `Replace HYPE/USD DCA with ETH/GBP`
   workflow on `main` only after typing both exact confirmations
   `MIGRATE_HYPE_TO_ETH_GBP` and `RAILWAY_CRON_PAUSED`. It audits Kraken order
   integrity, masks and hash-binds the complete source state, resets active
   analysis, starts ETH disabled, preserves BTC/SOL buy dates, and writes HYPE's
   final rule, analysis, and buy date to hash-bound `DCA_RETIRED_TARGET_STATE`.
   It intentionally carries HYPE's validated `LAST_BUY_DATE` to ETH solely to
   preserve the once-per-Bangkok-day allocation guard; it never copies HYPE
   analysis to ETH.
   Do not hand-edit analysis or execution state in the GitHub interface.
10. Run the `Kraken History Bootstrap` workflow with `targets=ETH_GBP` and wait
    for verified ETH/GBP history coverage. Do not reuse HYPE partitions for ETH.
11. Regenerate complete analysis with `!dca analyze all`, then run the Portfolio
    Balance Check and verify the new market set.
12. Enable only the desired pairs with Discord exact confirmation.
13. Restore `DCA_CRON_ENABLED=true` in Railway and verify `!dca health`.

Primary code locations:

- [Canonical targets and shared schema](dca_config.py#L18-L22)
- [Discord aliases and supported-pair validation](discord_bot.py#L132-L154)
- [Analysis pair parsing](crypto_analysis.py#L81-L117)
- [Kraken order-audit target list](kraken_order_audit.py#L21-L24)
- [Analysis workflow input](.github/workflows/crypto_analysis.yml#L7-L13)
- [Configuration workflow input](.github/workflows/update_dca_config.yml#L3-L17)
- [Guarded HYPE-to-ETH state migration](.github/workflows/migrate_hype_to_eth.yml)
- [Automated tests](tests)

Never permanently remove a pair that has a pending order. Reconcile it first.
For normal use, disabling is safer than a structural removal.

## Advanced policy changes

These changes require a tested pull request and cannot be made through Discord:

- Daily analysis time: [`.github/workflows/crypto_analysis.yml`](.github/workflows/crypto_analysis.yml#L3-L7).
  GitHub cron uses UTC; `7,37 21 * * *` is 04:07 and 04:37 Asia/Bangkok.
- Trend policy: [`crypto_analysis.py`](crypto_analysis.py#L229-L321).
- Emergency uptrend override state is maintainer-only, remains auditable in
  `DCA_UPTREND_OVERRIDE_STATE` and `DCA_ANALYSIS_STATE`, and its analysis-driven
  automatic release is gated on natural 3-close confirmation.
- Regime-to-budget policy: [`dca_config.py`](dca_config.py).
- Best-time policy: [`crypto_analysis.py`](crypto_analysis.py#L423-L500).
- Gemini explanation contract and model fallback:
  [`crypto_analysis.py`](crypto_analysis.py#L665-L688).
- Active direct-GBP execution and Kraken reconciliation:
  [`kraken_client.py`](kraken_client.py). The GBP-funded-USD connector remains
  only for legacy/historical recovery compatibility and is not an active route.

`DCA_CRON_ENABLED` is a Railway runtime variable, not a GitHub repository
variable. Keep it `true` for normal operation. Setting it to `false` pauses new
Railway scheduler dispatches and is intended for controlled maintenance.
`TIMEZONE=Asia/Bangkok` must match in both GitHub repository variables and the
Railway service environment.

## Troubleshooting

### Health says `ARMED`

This is expected before `DCA_START_DATE` or while waiting for the first 04:07
start-day analysis. Check the start date and the Crypto Analysis workflow.

### Health says `ATTENTION REQUIRED`

1. Run `!dca health` and note the named target or state problem.
2. Open the latest Crypto Analysis and Daily Crypto DCA workflow runs.
3. Check Railway deploy logs and confirm the deployed commit matches `main`.
4. Do not force a buy or reuse an old decision.
5. Keep the affected pair disabled until a fresh analysis and zero pending state
   are confirmed.

### Other health postures

- `ACTIVE`: enabled targets have fresh decisions and scheduling is running.
- `READY-BUT-DISABLED`: decisions are ready, but no target can submit a new
  order.
- `NOT READY`: configuration or state validation failed and trading is blocked.
- A pending intent is not permission to clear state manually; let the trader
  reconcile Kraken's open and closed orders.

### An old pending intent has no matching Kraken order

Do not delete execution JSON by hand or replay the missed date. The manual
`Recover Absent Kraken Intents` workflow handles reviewed, direct-GBP intents
older than 24 hours without placing or cancelling orders:

1. Temporarily set repository `DCA_TRADING_MODE=shadow`.
2. Run `preview`. Review the target dates, complete account-wide order scan,
   zero exact-ID and nearby-order observations, and `all_confirmed_absent=true`.
3. Run `apply` with the exact preview `state_hash`. It repeats the Kraken audit,
   rechecks shadow mode and unchanged execution state under the writer lock,
   and removes only the proven-absent pending records. Buy dates and outbox
   evidence are preserved; missed purchases are not marked filled or replayed.
4. Confirm no pending state remains, check available GBP, restore the prior
   trading mode, and check the next Daily Crypto DCA and holdings snapshot runs.

Any matching order, nearby untagged order, restricted history, incomplete page,
recent intent or state change stops recovery. A real order must use normal
reconciliation instead. New explicit Kraken rejection responses are reported as
safe no-fill failures; network failures and unknown errors remain locked.

### A budget change is blocked

Disable the pair first, wait for the workflow to finish, and retry the exact
command. Do not edit around the validation.

### Discord reports pending Portfolio ledger deliveries

Kraken remains the authoritative portfolio. A confirmed purchase remains in
`PENDING_GIST_DELIVERIES` until its exact private-repository audit row and
PortfolioEventV3 are present and acknowledged. The field name is deliberately
retained as local recovery evidence during the transport cutover. The Railway
controller retries it on the guarded schedule without calling Kraken or
blocking recovery of an existing order. Do not delete the queue manually;
Portfolio Compass will import the event idempotently. Ghostfolio remains an
optional mirror.

Before enabling any producer workflow, create a dedicated **private** GitHub
repository and an existing branch whose rules permit the outbox service
identity's compare-and-swap commits. Configure repository variables
`DCA_OUTBOX_REPOSITORY_OWNER`, `DCA_OUTBOX_REPOSITORY_NAME`,
`DCA_OUTBOX_REPOSITORY_BRANCH`, `DCA_OUTBOX_AUDIT_PATH`,
`DCA_OUTBOX_EVENT_PATH`, `DCA_OUTBOX_HOLDINGS_PATH`,
`DCA_OUTBOX_OPENING_BASIS_SOURCE_PATH` (ending in
`kraken_opening_basis_source_v1.json`), and `DCA_OUTBOX_OPENING_BASIS_PATH`
(ending in `kraken_opening_basis_v1.json`), plus the Actions secret
`DCA_OUTBOX_REPOSITORY_TOKEN`. This launch uses the existing classic controller
credential as a time-bounded compatibility value; rotate it after launch to a
fine-grained token limited to Contents read/write for that one private
repository and remove the classic value. Paths are repository-relative POSIX
paths and have no code defaults. The producer refuses missing
settings, public repositories, failed authorization, malformed files, and
unresolved SHA conflicts. `GIST_ID` and `GIST_TOKEN` do not enable a fallback.

### Import pre-cutover Kraken performance cost

Use the manual `Kraken Opening Performance Basis` workflow. This is a one-time
performance-book basis capture and is not a tax-basis calculation. It never
uses current ticker prices.

The compact artifact binds opening quantities to a separately reviewed,
immutable private-repository commit. It records the exact holdings/event paths,
blobs and content hashes at that commit, while the Kraken history source keeps
its own immutable commit. New events and rounded live snapshots therefore do
not re-derive or mutate the reviewed opening state.

1. Confirm the Kraken key has `Query closed orders & trades` and `Query ledger
   entries`, no withdrawal permission, and an unrestricted history start.
2. Choose one fixed UTC `generated_at`. Run `source` in `preview`, review the
   canonical hash and record counts, then run `source` in `publish` with the
   same timestamp and reviewed hash.
3. Record the source repository commit printed by the publish run. Run `basis`
   in `preview` with that same timestamp and source commit. Review every
   position's coverage and the compact canonical hash.
4. Run `basis` in `publish` with the identical timestamp, source commit, and
   hash. An exact retry is a no-op; different bytes at either fixed path are
   rejected and require an explicitly reviewed versioned supersession.

Only `complete` positions supply an opening-cost overlay to Portfolio Compass.
Deposits, transfers, rewards, sells, adjustments, missing ledgers, historical
USD funding that cannot be conserved from zero cash, and opening-quantity gaps
remain visibly `missing`. Do not fill those gaps with a current price or a
manually estimated exchange rate.

If the basis preview reveals that holdings-minus-events classified real
post-cutover Kraken activity as opening quantity, do **not** publish that basis.
Use the manual `Kraken Reviewed Account Recovery` workflow instead:

1. Configure the distinct write-once paths ending in
   `kraken_account_activity_source_v1.json` and
   `kraken_account_recovery_v1.json`.
2. Run `source / preview` with a fixed UTC `generated_at`. Review the exact
   trade, ledger, and order counts, then publish only the identical hash.
3. Record the published source commit. Run `recovery / preview` with that
   commit and the same timestamp.
4. Confirm the true-opening state hash, every summarized `buy` / `asset_out`
   row, and the reviewed ending-state hash. Publish only those identical bytes.

The reviewed seam is `(2026-08-06T04:21:00.000Z,
2026-08-07T03:31:59.999999Z]`, before the first canonical DCA event. The
producer makes no trading call. It accepts only completely linked direct-GBP
buys, exact paired `tradespot` ledger buys, and exact target withdrawals. An
asset-out quantity includes withdrawal principal plus its asset fee so the
consumer's remaining quantity and pro-rata performance cost stay aligned.

The Railway Discord controller reads the exact event ledger and Ghostfolio
event-receipt path from one resolved immutable commit. A dedicated
`DCA_OUTBOX_REPOSITORY_TOKEN` with Contents read access takes precedence; until
that narrower credential is configured, the already-present Railway `GH_PAT`
is used only for this count-and-hash health check. It no longer reads the
portfolio ledger or receipts from a Gist.

### Ghostfolio is empty or remains on loading placeholders

1. Open `C:\Users\anand\GLaDOS\Ghostfolio\Key.txt` and use that key for the
   localhost Ghostfolio login. Do not reuse an older exported key.
2. From the recovery repository, run
   `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ghostfolio\sync-canonical-key.ps1 -MinimumHoldings 3`.
   This fails unless the visible key and sync service resolve to the same
   populated Ghostfolio user.
3. Run `docker compose -f .\ghostfolio\compose.yml ps`. The app, PostgreSQL,
   Redis, and sync services must all be healthy. A failed sync health check can
   indicate Kraken quantity drift, missing `USDGBP` reporting data, a non-GBP
   custody account, or `portfolio/details` returning `hasError`.
4. Sign out of Ghostfolio, sign in with the current `Key.txt`, and reload
   `/en/home/holdings`. The expected active DCA rows are Bitcoin, Ethereum, and
   Solana. Historical Hyperliquid activity can remain for the fixed 7 August
   recovery record. Do not create another local user or copy activities between
   user IDs.

The signed holdings workflow runs at minutes 11 and 41. The local sidecar polls
every five minutes and reconciles a new signed snapshot idempotently, so either
component may be offline temporarily and catch up later without reaching
Kraken's trading API. Reconciliation is restricted to the `Kraken DCA` custody
account and cannot alter `Bitkub Legacy`; a snapshot older than a recorded DCA
fill is held until the next signed snapshot arrives. The health check fails if
the snapshot is missing, older than two hours, moves backwards, or has an
unfinished durable reconciliation receipt.

The sidecar completes any older durable holdings intent before importing a
newer DCA event. It rejects malformed or re-hashed event rows whose exact pair,
route, currencies, order IDs, timestamp, or numeric values do not match the
PortfolioEventV3 contract. This preserves automatic catch-up without allowing
an ambiguous offline state to double-count a holding.

If the confirmed 7 August HYPE purchase is missing its order-level Ghostfolio
activity, use the GitHub `Recover Confirmed Ghostfolio Event` workflow. First
store both exact Kraken order IDs in the temporary Actions secrets
`GHOSTFOLIO_RECOVERY_CRYPTO_ORDER_ID` and
`GHOSTFOLIO_RECOVERY_FUNDING_ORDER_ID`, then run `preview`. Run `publish` only
with both the canonical event hash and Markdown-row hash returned by that
preview. The incident-specific recovery uses Kraken reconciliation-only calls
and cannot submit an order; it will also refuse a value set that does not
exactly reproduce the existing ledger row.

Before `publish`, add the same order IDs plus the preview event hash to the
user-only `%LOCALAPPDATA%\dca-ghostfolio\secrets.env` as the two recovery
order-ID keys and `GHOSTFOLIO_RECOVERY_EVENT_HASH`. Run
`.\ghostfolio\write-service-env.ps1`, then rebuild and force-recreate the
`sync` service. The migration changes the existing HYPE opening balance to the
residual quantity and then imports the confirmed order; it must not create a
synthetic sale or alter the total Kraken quantity. After sidecar health and its
provenance state report `COMPLETE`, delete the three temporary local recovery
keys and the two temporary Actions secrets, regenerate `sync.env`, and recreate
`sync`. The permanent receipts make subsequent runs idempotent without those
temporary values.

### Local Ghostfolio backup task

Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
.\ghostfolio\install-backup-task.ps1 -RetentionCount 14` from the recovery
repository. The user-only task runs at 03:15, catches up a missed start, waits
briefly for Docker Desktop, retries bounded failures, and never overlaps an
existing run. It keeps the latest 14 backups only after a new dump passes a
disposable restore test. Check
`%LOCALAPPDATA%\dca-ghostfolio\backup-task-status.json`, the matching dump and
`.sha256` file, and Task Scheduler result `0` after registration or recovery.

## Safety rules

- Never give a Kraken API key withdrawal permission.
- Never paste secrets into Discord, JSON, source code, issues, or workflow logs.
- Never edit execution state to clear an order that has not been reconciled.
- Never force a live order by weakening dates, windows, or decision validation.
- Never manually trigger Daily Crypto DCA as a live test for an enabled, due
  target.
- If anything is uncertain, disable the affected pair and inspect health before
  changing state.

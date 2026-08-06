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

## Production baseline

The configured target set is exactly:

| Pair | `UPTREND` lower | `SIDEWAYS` midpoint | `DOWNTREND` higher | Intended state |
| --- | ---: | ---: | ---: | --- |
| `BTC/USD` | £10 | £15 | £20 | Enabled |
| `HYPE/USD` | £10 | £12.50 | £15 | Enabled |
| `SOL/USD` | £5 | £10 | £15 | Enabled |

- The bot deliberately buys more in a `DOWNTREND`, the midpoint in a
  `SIDEWAYS` market, and less in an `UPTREND`.
- Aggregate daily exposure is £50 / £37.50 / £25 when every pair is downtrend /
  sideways / uptrend respectively.
- Each enabled pair can buy at most once per Asia/Bangkok calendar day.
- The strict trading start gate is `DCA_START_DATE=2026-08-07` in
  `Asia/Bangkok`. Earlier orders are blocked.

This table is the approved baseline, not a substitute for checking live state.
Use `show status` and `!dca health` in Discord whenever you need the current
rules, decisions, pending state, and scheduler posture.

## What happens each day

1. GitHub Actions is scheduled for 04:00 Asia/Bangkok and analyzes completed
   Kraken candles for all configured pairs. GitHub may queue a scheduled run a
   few minutes after 04:00.
2. Deterministic Python classifies each pair as `UPTREND`, `DOWNTREND`, or
   `SIDEWAYS`.
3. Python selects the higher / midpoint / lower GBP spend for downtrend /
   sideways / uptrend respectively, plus the best 15-minute execution time from
   deterministic 3-, 5-, and 7-day timing windows.
4. The workflow writes a fresh `DCA_ANALYSIS_STATE` and posts a readable summary
   to Discord.
5. Railway checks the absolute execution times every five minutes and dispatches
   the trader when a pair is due.
6. The trader rechecks the live rule, decision, date, window, Kraken minimum,
   pending-order state, and once-per-day guard.
7. It sells the exact GBP budget on `GBP/USD`, waits for confirmed net USD, and
   spends that USD on the selected crypto/USD pair.
8. Kraken remains the authoritative record and Discord receives the result.

Gemini Flash-Lite explains the completed Python decision. It cannot select or
change the regime, amount, pair, or execution time. If Gemini is unavailable,
the deterministic decision remains valid and only the optional explanation is
missing.

Missing, stale, insufficient, or failed analysis creates an `ERROR` decision,
alerts Discord, and skips that purchase. The bot never reuses an old decision.

## Everyday Discord controls

Commands are exact safety controls. Use the allowlisted Discord account and the
exact lowercase `!dca ` prefix and spacing.

### Check the bot

```text
show status
!dca health
```

Before the configured start day or before its first 04:00 analysis, a pair may
show an `ERROR` placeholder while overall health correctly reports `ARMED`.
After a successful daily analysis, enabled pairs should show fresh `READY`
decisions, regimes, and execution times.

### Change both budgets for an existing pair

Example: change BTC to a £12 lower endpoint and £25 higher endpoint. Sideways
will then use the derived £18.50 midpoint.

```text
!dca disable BTC
```

Wait for the first **Update DCA Configuration** run to succeed, then send:

```text
!dca set BTC amounts to 12 low and 25 high
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
!dca confirm enable BTC_USD
```

Wait for the final **Update DCA Configuration** run to succeed. Railway can take
up to five minutes to refresh the new schedule. Finish with:

```text
show status
!dca health
```

Replace `BTC` with `HYPE` or `SOL` as needed. Enter numbers without a `£` sign
and with no more than two decimal places. The lower amount cannot exceed the
higher amount. Both endpoints must be between £5 and £1,000 and at or above
Kraken's current market minimum before enabling. Zero is permitted only as a
disabled placeholder. Sideways uses `(lower + higher) / 2`, rounded to the
nearest penny with half-up currency rounding.

### Stop buying a pair

```text
!dca disable HYPE
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

## Rules JSON and state ownership

The user-owned repository variable is `DCA_TARGET_MAP`. Its approved shape is:

```json
{
  "BTC_USD": {
    "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
    "BUY_ENABLED": true
  },
  "HYPE_USD": {
    "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 15},
    "BUY_ENABLED": true
  },
  "SOL_USD": {
    "REGIME_AMOUNTS_GBP": {"LOW": 5, "UP": 15},
    "BUY_ENABLED": true
  }
}
```

For compatibility, the JSON field `LOW` stores the lower endpoint and `UP`
stores the upper/higher endpoint. `UP` does **not** mean the amount used in an
uptrend. Analysis records the derived tiers `LOW`, `MID`, or `HIGH` according to
the counter-cyclical policy.

Use Discord for routine changes. It validates and serializes the write, checks
fresh analysis before enabling, and checks Kraken's current market minimum.

If Discord is unavailable, the
[Update DCA Configuration workflow](https://github.com/aesscialo-bot/DCA-Bot/actions/workflows/update_dca_config.yml)
can safely perform these limited operations:

- Disable: `action=set_enabled`, canonical `symbol`, `enabled_json=false`.
- Change budgets while disabled: `action=set_amounts`, canonical `symbol`, and
  numeric `low_amount_gbp_json` / `up_amount_gbp_json` values.
- Validate without writing: `action=dry_run`, canonical `symbol`, both numeric
  LOW/UP inputs, and a target that is already disabled.

The workflow input `up_amount_gbp_json` is a compatibility name for the
upper/higher endpoint.

Do not manually use `set_enabled=true`; safe enabling binds to fresh decision
and rules fingerprints that Discord supplies during exact confirmation.

Do not manually edit:

- `DCA_ANALYSIS_STATE`: owned by the analysis workflow.
- `DCA_EXECUTION_STATE`: owned by the trader; contains pending-order recovery
  and the durable Portfolio Compass ledger-delivery queue.
- API keys or tokens in JSON, Discord, source files, or public logs.

Changing either endpoint changes its mapped regime amount and can change the
derived midpoint; every endpoint change invalidates that target's old decision
fingerprint. Enabling or disabling changes the globally reviewed rules state.
Run fresh analysis after a budget change before trying to re-enable or trade.

## Adding or permanently removing a pair

Pair membership is a maintainer/Codex code-and-state migration, not a beginner
JSON setting. The current schema requires exactly `BTC_USD`, `HYPE_USD`, and
`SOL_USD`; an extra or missing key fails closed.

For a permanent pair change:

1. Disable every target through Discord.
2. Set `DCA_CRON_ENABLED=false` in Railway service variables.
3. Run a read-only Kraken order audit and confirm there are no unresolved or
   pending intents. Never discard an order or pending state to make this pass.
4. Confirm the new `BASE/USD` spot market exists on Kraken, can be funded from
   `GBP/USD`, and satisfies the current Kraken minimum.
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
9. Run the authorized migration mechanism. Do not hand-edit analysis or
   execution state in the GitHub interface.
10. Regenerate complete analysis with `!dca analyze all`, then run the Portfolio
    Balance Check and verify the new market set.
11. Enable only the desired pairs with Discord exact confirmation.
12. Restore `DCA_CRON_ENABLED=true` in Railway and verify `!dca health`.

Primary code locations:

- [Canonical targets and shared schema](dca_config.py#L18-L22)
- [Discord aliases and supported-pair validation](discord_bot.py#L132-L154)
- [Analysis pair parsing](crypto_analysis.py#L81-L117)
- [Kraken order-audit target list](kraken_order_audit.py#L21-L24)
- [Analysis workflow input](.github/workflows/crypto_analysis.yml#L7-L13)
- [Configuration workflow input](.github/workflows/update_dca_config.yml#L3-L17)
- [Automated tests](tests)

Never permanently remove a pair that has a pending order. Reconcile it first.
For normal use, disabling is safer than a structural removal.

## Advanced policy changes

These changes require a tested pull request and cannot be made through Discord:

- Daily analysis time: [`.github/workflows/crypto_analysis.yml`](.github/workflows/crypto_analysis.yml#L3-L7).
  GitHub cron uses UTC; `0 21 * * *` is 04:00 Asia/Bangkok.
- Trend policy: [`crypto_analysis.py`](crypto_analysis.py#L229-L321).
- Regime-to-budget policy and midpoint rounding: [`dca_config.py`](dca_config.py).
- Best-time policy: [`crypto_analysis.py`](crypto_analysis.py#L423-L500).
- Gemini explanation contract and model fallback:
  [`crypto_analysis.py`](crypto_analysis.py#L665-L688).
- GBP-funded USD execution and Kraken reconciliation:
  [`kraken_client.py`](kraken_client.py).

`DCA_CRON_ENABLED` is a Railway runtime variable, not a GitHub repository
variable. Keep it `true` for normal operation. Setting it to `false` pauses new
Railway scheduler dispatches and is intended for controlled maintenance.
`TIMEZONE=Asia/Bangkok` must match in both GitHub repository variables and the
Railway service environment.

## Troubleshooting

### Health says `ARMED`

This is expected before `DCA_START_DATE` or while waiting for the first 04:00
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

### A budget change is blocked

Disable the pair first, wait for the workflow to finish, and retry the exact
command. Do not edit around the validation.

### Discord reports pending Portfolio ledger deliveries

Kraken remains the authoritative portfolio. A confirmed purchase remains in
`PENDING_GIST_DELIVERIES` until its exact private-Gist ledger row is present and
acknowledged. The Railway controller retries it on the guarded schedule without
calling Kraken or blocking recovery of an existing order. Do not delete the
queue manually; Portfolio Compass will import the row idempotently. Ghostfolio
remains an optional mirror.

## Safety rules

- Never give a Kraken API key withdrawal permission.
- Never paste secrets into Discord, JSON, source code, issues, or workflow logs.
- Never edit execution state to clear an order that has not been reconciled.
- Never force a live order by weakening dates, windows, or decision validation.
- Never manually trigger Daily Crypto DCA as a live test for an enabled, due
  target.
- If anything is uncertain, disable the affected pair and inspect health before
  changing state.

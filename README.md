# Smart DCA Automation (Multi-Symbol Analysis + Execution)

A complete system that analyzes market data to find the best time of day to buy **multiple cryptocurrencies**, executes spot market orders on **Kraken in GBP**, and exposes natural-language controls through a Discord bot hosted continuously on Railway.

The system consists of the following files:

| File | Role |
|------|------|
| `kraken_client.py` | Kraken spot client — CCXT authentication, THB-budget-to-GBP conversion, market validation, order placement, and fill normalization. |
| `bitkub_client.py` | Legacy Bitkub client used by the separate portfolio balance/report workflow and THB/USD logging helpers. It is not used to place trades on the Kraken path. |
| `crypto_analysis.py` | Daily market analysis using CCXT + Gemini AI. Updates `DCA_TARGET_MAP` with optimal buy times. |
| `crypto_dca.py` | Trade executor — reads `DCA_TARGET_MAP`, places Kraken GBP market buys, and logs to Gist + Ghostfolio. |
| `portfolio_balance.py` | Legacy Bitkub portfolio reporter — fetches Bitkub balances and reports THB/USD values. It does not currently report the Kraken wallet. |
| `portfolio_logger.py` | Logs individual trades to Ghostfolio portfolio tracker. |
| `gist_logger.py` | Appends trade records to a GitHub Gist as a markdown ledger. |
| `discord_bot.py` | Railway-hosted Discord bot — natural-language control of workflows and DCA config, plus the built-in DCA scheduler. |

**Workflows** (`.github/workflows/`):

1. **`crypto_analysis.yml`** — Runs daily (04:00 BKK / 21:00 UTC). Analyzes **60 days** of price data across **4 periods** (14, 30, 45, 60 days) for **all pairs in `DCA_TARGET_MAP`** to find the "Champion Time" for each. Uses AI synthesis to pick optimal buy time. Updates `DCA_TARGET_MAP`.
2. **`daily_dca.yml`** — Triggered by the Railway scheduler, manual dispatch, or the **daily 22:00 UTC safety net** (05:00 Bangkok). Checks enabled symbols and executes Kraken `COIN/GBP` market buys using a THB-denominated strategy budget converted to GBP immediately before ordering. GitHub cron schedules run from the default branch, so this Kraken safety net becomes active after the branch is merged.
3. **`portfolio_check.yml`** — Legacy Bitkub reporting workflow. Runs **monthly on the 5th at 07:00 BKK** (00:00 UTC), on manual dispatch, and on every push to `main`. It does not read Kraken balances.

## System Orchestration

```mermaid
flowchart LR
    U["Discord user"] --> DG["Discord Gateway"]
    DG --> RB["Railway<br/>discord_bot.py"]
    RB -->|"read/write"| CFG["GitHub variable<br/>DCA_TARGET_MAP"]
    RB -->|"workflow_dispatch<br/>configured ref"| W1
    RB -->|"scheduled dispatch<br/>-30/+60 min window"| W2
    RB -->|"portfolio command"| W3

    C1["Daily 21:00 UTC"] --> W1["crypto_analysis.yml"]
    C2["Daily 22:00 UTC safety net<br/>default branch only"] --> W2["daily_dca.yml"]
    C3["Monthly / push to main<br/>/ manual"] --> W3["portfolio_check.yml"]

    W1 -->|"BTC_THB → BTC/USDT"| BIN["Binance US market data"]
    W1 --> GEM["Gemini analysis"]
    W1 --> CFG

    W2 -->|"read rules / write LAST_BUY_DATE"| CFG
    W2 -->|"THB budget → GBP"| FX["Frankfurter<br/>Open ER API fallback"]
    W2 -->|"COIN/GBP market buy"| KR["Kraken spot"]
    W2 --> LOG["Optional post-fill logging<br/>Ghostfolio + GitHub Gist"]

    W3 -->|"legacy balances and history"| BK["Bitkub"]
    W1 --> DIS["Discord Webhook<br/>notifications"]
    W2 --> DIS
    W3 --> DIS
```

The trading and portfolio paths are intentionally shown separately: `daily_dca.yml` trades on Kraken in GBP, while `portfolio_check.yml` is still a legacy Bitkub report until it is migrated.

For an unmerged branch deployment, set Railway's `GITHUB_WORKFLOW_REF` to that branch so Discord-triggered runs use its code. GitHub's scheduled events always use the default branch; they will not run the Kraken workflow until this branch is merged into `main`.

## Features

- **Multi-Symbol Support**: Analyze and trade multiple pairs independently (e.g., BTC at 23:00, LINK at 23:45).
- **Self-Optimizing**: Buy time adjusts daily based on 60-day historical analysis with AI-powered recommendations.
- **Configurable Report Verbosity**: Analysis workflow supports short (AI summary only) or full (detailed breakdown) Discord reports.
- **Kraken GBP Execution**: Converts each THB-denominated strategy budget to GBP at execution time and submits a Kraken `COIN/GBP` spot market order.
- **Legacy Portfolio Balance Tracking**: The separate Bitkub workflow reports Bitkub holdings in THB and USD; it is not a Kraken wallet report.
- **Multi-Layer Safeguards**: Prevents double-buying with `LAST_BUY_DATE` tracking and workflow concurrency control.
- **Detailed Logging**: Kraken fills are normalized back to THB and USD for the existing GitHub Gist and Ghostfolio records; Discord also shows the actual GBP spent.
- **Portfolio Integration**: Automatic trade logging to Ghostfolio portfolio tracker with 8-decimal precision and timezone-aware timestamps.
- **Discord Integration**: Real-time notifications for trades (with THB+USD amounts and Ghostfolio status), errors, and critical alerts including FX rate failures.
- **Timezone Aware**: Fully configurable timezone support via `TIMEZONE` env variable (defaults to Asia/Bangkok).
- **Non-Blocking Logging**: Trade execution succeeds even if Gist or Ghostfolio logging fails (errors logged and notified).

### 1. Secrets (Secure Storage)
Go to `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`:

| Secret Name | Value Description |
| :--- | :--- |
| `KRAKEN_API_KEY` | Kraken API key used by `daily_dca.yml` for spot trading. |
| `KRAKEN_API_SECRET` | Kraken API secret used by `daily_dca.yml`. |
| `BITKUB_API_KEY` | Bitkub API key used only by the legacy `portfolio_check.yml` workflow. |
| `BITKUB_API_SECRET` | Bitkub API secret used only by the legacy portfolio workflow. |
| `GEMINI_API_KEY` | Google AI Studio Key. |
| `DISCORD_WEBHOOK_URL` | Your Discord Webhook URL. |
| `GH_PAT_FOR_VARS` | Personal Access Token (Classic) with `repo` and **`gist`** scope. Used to update variables and write to your log. |
| `GIST_TOKEN` | (Same as GH_PAT_FOR_VARS) Token used specifically by the python script to update Gists. |
| `GIST_ID` | The ID of your `trade_log.md` gist. |
| `GHOSTFOLIO_TOKEN` | Your Ghostfolio access token for portfolio logging. |

### 2. Variables (Configuration)
Go to `Settings` -> `Secrets and variables` -> `Actions` -> `New repository variable`:

| Variable Name | Example Value | Description |
| :--- | :--- | :--- |
| `DCA_TARGET_MAP` | `{"BTC_THB": {"TIME": "07:00", "AMOUNT": 800, "BUY_ENABLED": true, "LAST_BUY_DATE": "", "DYNAMIC_DCA": {"ENABLED": true, "THRESHOLD_PERCENT": -2, "REDUCED_MULTIPLIER": 0.5}}}` | **Key config.** `*_THB` and `AMOUNT` remain strategy/config conventions. The Kraken executor converts `AMOUNT` from THB to GBP and maps `BTC_THB` to `BTC/GBP`. |
| `TIMEZONE` | `Asia/Bangkok` | Timezone for operations. |
| `PORTFOLIO_ACCOUNT_MAP` | `{"BTC": "3cced5d3-f219-47c8-bb73-878466060d7a", "DEFAULT": "9069984b-3c2b-48d8-831d-b7d73b5bafb7"}` | Maps crypto symbols to Ghostfolio account IDs. Falls back to DEFAULT if symbol not found. |
| `GHOSTFOLIO_URL` | `https://ghostfol.io` | Ghostfolio instance URL (optional, defaults to https://ghostfol.io). |

### Dynamic DCA

Set `DYNAMIC_DCA.ENABLED` to `true` for each asset that should use the policy.
The bot reads the same lifetime, account-scoped ROI shown in Ghostfolio's
holdings table before placing the order:

- ROI at or above `THRESHOLD_PERCENT` (default `-2`) buys `AMOUNT * REDUCED_MULTIPLIER` (default `0.5`).
- ROI below the threshold buys the configured `AMOUNT` at `x1`.
- Missing, invalid, or unavailable Ghostfolio ROI also buys the configured `AMOUNT` at `x1`.
- If the reduced strategy amount would be below the internal 10 THB floor, the bot falls back to the configured `AMOUNT` at `x1`.
- Kraken's live minimum order cost and minimum base amount are checked after conversion to GBP. The order is rejected safely if the GBP budget is too small.

Every successful DCA Discord notification includes the asset ROI and the full- or half-buy reason.

### 3. Workflow Configuration

**Analysis Workflow (`crypto_analysis.yml`)**:
- **Schedule**: Daily at 21:00 UTC (04:00 Bangkok)
- **Trigger**: Daily schedule or manual dispatch
- **Concurrency**: Only one analysis runs at a time (cancel-in-progress)
- **Environment**: Uses `binanceus` exchange to avoid geo-restrictions
- **Symbol Resolution**: Automatically derives symbols from `DCA_TARGET_MAP` keys (e.g., `BTC_THB` → `BTC/USDT`). Override with explicit `symbol` input on manual dispatch.
- **Report Mode**: Configurable via `short_report` input (default: true)
  - **Short Report (true)**: Sends AI summary only (~8 lines) - ideal for daily automated runs
  - **Full Report (false)**: Sends detailed analysis with all time period breakdowns - use for deep dives

**Trader Workflow (`daily_dca.yml`)**:
- **Trigger**: Railway/manual dispatch + **daily 22:00 UTC cron** (05:00 Bangkok). The cron is a limited catch-up for targets already passed by 05:00; it cannot cover later target times if Railway is down.
- **Concurrency**: Only one trade workflow runs at a time (queued, not cancelled)
- **Pre-Check**: Bash Quick Check runs first (no checkout/Python needed). Only checks out code and installs dependencies if a trade is needed
- **Safeguards**: Multiple layers check `BUY_ENABLED`, `LAST_BUY_DATE`, and time window
- **Exchange**: `TRADING_EXCHANGE=kraken` and `KRAKEN_QUOTE_CURRENCY=GBP`; config symbols map from `COIN_THB` to Kraken `COIN/GBP`
- **Budgeting**: `AMOUNT` is a THB-denominated strategy budget converted to GBP immediately before the order

**Portfolio Balance Workflow (`portfolio_check.yml`)**:
- **Scope**: Legacy Bitkub-only reporting. It does not fetch Kraken balances or Kraken order history.
- **Schedule**: Monthly on the 5th at 07:00 Bangkok time (00:00 UTC)
- **Trigger**: Also runs on every push to main + manual dispatch available
- **Optimized**: Only installs minimal dependencies (requests library), uses pip caching for speed
- **Report Mode**: Adaptive based on trigger
  - **Short Report (push)**: Current holdings and total value only - fast status check
  - **Monthly Full Report (5th)**: Current holdings plus the exact previous month's trade history (5th 07:00 BKK → 5th 07:00 BKK)
  - **Manual dispatch (GitHub Actions UI)**: Full monthly report by default (`short_report` input defaults to `false`)
  - **Manual dispatch (Discord Bot)**: Short report by default — say "full portfolio" or "portfolio with trades" for the full monthly report
- **Report**: Fetches balances for all coins in DCA_TARGET_MAP, calculates portfolio value, sends Discord notification

## How It Works

### Daily Analysis Cycle

```mermaid
flowchart TD
    A["Trigger<br/>daily 21:00 UTC<br/>or manual dispatch"] --> B["Resolve symbols from DCA_TARGET_MAP<br/>BTC_THB → BTC/USDT"]
    B --> C["Fetch 60 days of 15-minute OHLCV<br/>from Binance US through CCXT"]
    C --> D["Analyze 14 / 30 / 45 / 60 days<br/>median_miss, win_rate, dca_price"]
    D --> E["Gemini synthesis<br/>select RECOMMENDED_TIME"]
    E --> F{AI succeeded?}
    F -->|Yes| G["Use AI time"]
    F -->|No| H["Use 30-day quantitative best time"]
    G --> I["Send per-symbol Discord report<br/>short summary or full tables"]
    H --> I
    I --> J["Update in-memory TIME<br/>for existing COIN_THB entries"]
    J --> K["Emit best_time_map<br/>through GITHUB_OUTPUT"]
    K --> L["Workflow fetches the live map<br/>and merges TIME fields only"]
```

1. At 04:00 Bangkok time, `crypto_analysis.yml` triggers
2. Resolves symbols from `DCA_TARGET_MAP` keys (e.g., `BTC_THB` → `BTC/USDT`, `LINK_THB` → `LINK/USDT`, `SUI_THB` → `SUI/USDT`). Can be overridden via explicit input.
3. Fetches 60 days of 15-minute OHLCV data from Binance US for each symbol
4. Calculates metrics: `median_miss`, `win_rate`, `dca_price` for each 15-min slot
5. Gemini AI synthesizes recommendation across 14/30/45/60-day periods
6. Sends Discord report (short AI summary by default, full analysis if configured)
7. Updates `DCA_TARGET_MAP["<SYMBOL>_THB"]["TIME"]` with optimal buy time for each pair

### Trade Execution Cycle

```mermaid
flowchart TD
    A["Trigger<br/>Railway scheduler, Buy Now,<br/>manual, or 22:00 UTC safety net"] --> B["Bash quick check<br/>no checkout required"]
    B --> C{Enabled, not handled today,<br/>and target time reached?}
    C -->|No| D["Exit before setup"]
    C -->|Yes| E["Checkout, Python 3.12,<br/>install dependencies"]
    E --> F["Python validates time window<br/>and LAST_BUY_DATE"]
    F --> G{Already handled today?}
    G -->|Yes| H["Skip symbol"]
    G -->|No| ROI["Optional Dynamic DCA<br/>read Ghostfolio ROI"]
    ROI --> I["Convert THB strategy budget to GBP<br/>validate Kraken COIN/GBP minimums"]
    I --> J["Submit Kraken market buy<br/>wait 5 seconds and fetch fill"]
    J --> K["Normalize fill to THB and USD<br/>for existing logs"]
    K --> L["Log to Ghostfolio<br/>non-blocking"]
    L --> M["Log to GitHub Gist<br/>non-blocking"]
    M --> N["Send Discord result<br/>GBP, THB, USD, fill, ROI"]
    I -->|"validation / FX error"| ERR["Send Discord failure"]
    J -->|"order / fill error"| ERR
    N --> O["Write LAST_BUY_DATE<br/>3 retries; fail loudly"]
    ERR --> O
```

1. **Trigger** via built-in DCA scheduler (-30/+60 min window), daily 22:00 UTC safety net cron, Discord "Buy Now" command, or manual GitHub Actions UI dispatch
2. **Bash Quick Check** (no checkout/Python required): Filters by `BUY_ENABLED`, `LAST_BUY_DATE`, time window
3. If no match → Workflow exits before checkout, Python setup, or dependency installation
4. If match found → Checkout repo → Setup Python → Install deps → Run Python
5. **Python**: Validates the time window (±5 minutes or catch-up), checks `LAST_BUY_DATE`, and optionally adjusts the budget using Ghostfolio ROI.
6. Converts the THB-denominated strategy budget to GBP using Frankfurter with Open ER API fallback.
7. Maps the config symbol to Kraken (`BTC_THB` → `BTC/GBP`), validates Kraken's live market minimums, and submits a market buy by quote cost.
8. Waits five seconds, fetches the confirmed Kraken fill, and normalizes the GBP spend to THB and USD for the existing logs.
9. **Logs to Ghostfolio and Gist** (non-blocking) and sends a Discord result containing the Kraken pair, actual GBP spend, normalized THB/USD values, fill and Dynamic DCA reason.
10. Writes `LAST_BUY_DATE` with three retries and fails loudly if the repository-variable update fails. This date is written after an order error too, preventing retries from hammering a broken API or an underfunded account for the rest of that day.

**Daily Safety Net**: The 22:00 UTC cron runs at 05:00 Bangkok and catches enabled targets that have already passed since local midnight. It does not cover target times later than 05:00 if Railway is unavailable. The bash quick check uses `DIFF >= -5`, while `LAST_BUY_DATE` prevents duplicate attempts when Railway already dispatched the workflow.

**Automating the trigger**: The continuously running Railway bot is the primary scheduler. It dispatches the workflow reference in `GITHUB_WORKFLOW_REF`; use `main` after the Kraken pull request is merged, or the feature branch while validating an unmerged build. The daily GitHub cron remains the safety net.

## Legacy Bitkub Portfolio Balance Reporting

This independent workflow still reads the Bitkub wallet and Bitkub order history. It does not confirm Kraken GBP holdings and is retained only for backward-compatible reporting:

It requires `BITKUB_API_KEY` and `BITKUB_API_SECRET`; without those legacy secrets, the Discord portfolio command will fail rather than report Kraken holdings.

```mermaid
flowchart TD
    A1["Monthly cron<br/>5th at 07:00 BKK"] -->|SHORT_REPORT=false| C
    A2["📤 Push to main"] -->|SHORT_REPORT=true| C
    A3["GitHub Actions UI<br/>manual dispatch"] -->|SHORT_REPORT=false default| C
    A4["Railway Discord bot"] -->|"short by default<br/>full when requested"| API["GitHub workflow_dispatch API"]
    API --> C

    C["Fetch Bitkub wallet balances<br/>/api/v3/market/balances"]
    C --> D["Fetch current prices<br/>Bitkub TradingView API"]
    D --> E["Fetch THB→USD rate<br/>two-source fallback"]
    E --> F["Build Bitkub current holdings<br/>THB and USD values"]
    F --> G{SHORT_REPORT?}
    G -->|true| H["Send short Discord report<br/>holdings and total only"]
    G -->|false| I["Compute previous<br/>5th-to-5th window"]
    I --> J["Fetch up to 200 Bitkub orders per coin<br/>filter filled buys in window"]
    J --> K["Build Bitkub trade history<br/>with historical THB→USD FX"]
    K --> L["Send full Discord report<br/>holdings and trade history"]
```

### Legacy report features
- **Multi-Coin Support**: Automatically fetches balances for all coins in `DCA_TARGET_MAP`
- **Real-Time Pricing**: Gets current market prices from Bitkub API
- **Dual Currency**: Shows values in both THB and USD
- **Configurable Report Verbosity**: Short (balance only) or full (with trade history)
- **Automated Reports**: Monthly full report on the 5th + short balance-only report on every push to main
- **Discord Notifications**: Formatted report with individual coin balances and total portfolio value

### Report Format

**Short Report** (on push):
```
📊 CURRENT HOLDINGS

BTC
  Amount: 0.00084835
  Price: ฿2,113,889.19
  Value: ฿1,793.32 ($57.41)

LINK
  Amount: 5.01449152
  Price: ฿277.11
  Value: ฿1,389.57 ($44.48)

💰 Total Portfolio Value
฿3,182.88
$101.89
```

**Full Report** (monthly schedule or manual):
Includes all of the above PLUS:
```
════════════════════════════════════════
📈 TRADE HISTORY (Feb 05 → Mar 05, 2026)

BTC (19 trades) — Crypto amount: 0.00446910 — Spent: ฿6,949.95 ($223.75)
• 2026-03-04 23:00 +07 - 0.00042594 BTC - Order ID: 69a04e8a93 - Price: ฿2,112,938.03 ($68,036.6000) - Spent: ฿899.99 ($28.98)
...

LINK (10 trades) — Crypto amount: 14.66775216 — Spent: ฿2,400.00 ($77.24)
• 2026-03-04 23:45 +07 - 1.04260791 LINK - Order ID: 69a04e9aa0 - Price: ฿287.74 ($9.2668) - Spent: ฿300.00 ($9.66)
...
```

### Schedule
- **Monthly (Full Report)**: Every 5th of the month at 07:00 Bangkok time - includes exact 5th-to-5th trade history
- **On Push (Short Report)**: After every commit to main branch - balance only for quick checks
- **Manual (Short Report by default)**: Can be triggered via GitHub Actions UI or Discord bot — shows balance only unless full report is explicitly requested

## Currency Conversion

The Kraken trader fetches THB→GBP immediately before an order, then normalizes the confirmed GBP fill back to THB for the existing THB/USD logs. The legacy reporter and loggers fetch THB→USD. Both paths use:
- **Primary**: Frankfurter API (`api.frankfurter.app`)
- **Secondary**: Open Exchange Rate API (`open.er-api.com`)
- **Trade behavior**: A THB→GBP failure stops the Kraken order safely; a post-trade THB→USD failure leaves USD log values at `$0.00` and sends a Discord warning.

## Portfolio Logging

Trades are automatically logged to Ghostfolio for portfolio tracking:
- **Account Mapping**: Maps crypto symbols to Ghostfolio accounts via `PORTFOLIO_ACCOUNT_MAP` (falls back to DEFAULT)
- **Precision**: 8-decimal quantity formatting (e.g., 0.00012345 BTC), 4-decimal USD unit price (e.g., $0.8895 SUI)
- **Comment Format**: `฿800.00 - $25.10 - tx_abc123de` (shows THB, USD, and exchange order ID)
- **Data Source**: Yahoo Finance (BTCUSD, LINKUSD, etc.) - free tier compatible
- **Timezone Support**: Uses configured TIMEZONE, converts to UTC for Ghostfolio
- **Timeout**: 30 seconds for all Ghostfolio API requests (doubled from standard)
- **Error Handling**: Non-blocking - trade executes even if Ghostfolio fails (errors logged to console and Discord)
- **Gist Integration**: "Saved" column reflects Ghostfolio logging success (`true`/`false`)

## Safeguards Against Double-Buying

| Layer | Location | Check | Prevents |
|-------|----------|-------|----------|
| **Concurrency** | GitHub Actions | Only 1 workflow runs at a time | Race conditions |
| **Bash Filter** | Quick Check step | `LAST_BUY_DATE == today` | Unnecessary Python execution |
| **Python Filter** | Symbol processing | `BUY_ENABLED == false` | Disabled symbols |
| **Time Window** | `is_time_to_trade()` | Within ±5 min or catch-up | Out-of-window execution |
| **Date Check** | Per-symbol loop | `LAST_BUY_DATE == today` | Same-day duplicate |
| **API Update** | Post-attempt (success or failure) | 3 retries, fail loudly | Repeated same-day attempts and silent state failure |

## Discord Bot (Natural Language Control)

A continuously running Discord bot (`discord_bot.py`) hosted on Railway that lets you control the DCA system through natural-language chat. Local Docker remains available for development, but should not run at the same time as the Railway service with the same token.

### Capabilities
- **Trigger Analysis**: "Run analysis" (analyzes all symbols in DCA config) / "Analyze BTC" (specific symbol)
- **Check Portfolio**: "Show balance" / "Portfolio report"
- **View Config**: "Show status" / "What's the current config?"
- **View Accounts**: "Show accounts" / "Portfolio account map"
- **Update DCA Config**: "Set BTC amount to 600" / "Set BTC time to 22:00" / "Disable LINK"
- **Buy Now**: "Buy LINK now" / "Purchase SUI immediately" — sets `TIME` to the current local `HH:MM`, enables the symbol, and dispatches the configured workflow reference immediately

All commands are interpreted via Gemini AI — just type naturally.

### Setup

1. **Create a Discord Application** at [discord.com/developers](https://discord.com/developers/applications)
2. Under **Bot** settings, enable **Message Content Intent**
3. Generate a **Bot Token** and invite the bot to your server (Send Messages, Read Messages permissions)
4. Install dependencies: `pip install -r bot_requirements.txt`
5. Set environment variables and run:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
export GEMINI_API_KEY="your-gemini-key"
export GH_PAT="your-github-pat"           # Same PAT as GH_PAT_FOR_VARS (repo scope)
export GITHUB_REPO="owner/repo"            # e.g. "simon/DCA-Analysis"
export GITHUB_WORKFLOW_REF="main"           # Use a feature branch only while validating an unmerged build
export DISCORD_CHANNEL_ID="123456789"      # Optional: restrict to one channel
export DISCORD_ALLOWED_USERS="111,222"     # Optional: restrict to specific Discord user IDs
export DCA_CRON_ENABLED="true"             # Optional: enable built-in DCA scheduler
export TIMEZONE="Asia/Bangkok"             # Optional: timezone for scheduler (default: Asia/Bangkok)

python discord_bot.py
```

### Behaviour
- **With `DISCORD_CHANNEL_ID` set**: Bot responds to all messages in that channel
- **Without it**: Bot only responds to @mentions and DMs
- **With `DISCORD_ALLOWED_USERS` set**: Only listed user IDs can trigger actions
- **DCA updates are validated**: AMOUNT must be 50–2000 THB, inclusive; TIME must be HH:MM; BUY_ENABLED must be bool. Cannot add/remove symbols — only update existing ones.

### Built-in DCA Scheduler
When `DCA_CRON_ENABLED=true`, the bot replaces the need for an external cron service (e.g., cron-job.org) by dispatching `daily_dca.yml` at the right times:
- Dispatches the branch or tag configured by `GITHUB_WORKFLOW_REF` (default `main`)
- Reads target buy times from `DCA_TARGET_MAP` and triggers the workflow within a **-30/+60 min window** (clock-aligned ticks at :00, :15, :30, :45), giving ~7 attempts per target to handle GitHub Actions flakiness
- Status and update commands show planned dispatch times so you can verify the schedule at a glance
- Schedule refreshes **every 30 minutes**, on **startup**, and **opportunistically** whenever any Discord command reads/updates `DCA_TARGET_MAP`. A Discord notification is sent if the schedule changes or if the GitHub API call fails during refresh
- The `daily_dca.yml` bash quick-check still handles all safety logic (time matching, double-buy prevention), so early triggers exit cheaply

### Hosting
The production Discord process runs as the Railway `discord-bot` service built from this repository's `Dockerfile`. Railway stores its runtime configuration as protected service variables and automatically redeploys the configured GitHub branch. The process needs no public HTTP endpoint because it maintains an outbound Discord gateway connection.

For local development, use `docker compose up -d --build`, but stop the local container before starting Railway to avoid two processes competing for the same Discord bot session.


# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent Rules

1. **NO AUTOMATED COMMITS OR PUSHES** — all git operations must be read-only unless the user explicitly commands otherwise.
2. **NEVER READ SECRETS** — do not open `run_bot.sh`, `.env`, or any file in `.gitignore` that may contain hardcoded secrets/tokens.

## Project Overview

Python 3.12 cryptocurrency DCA (Dollar-Cost Averaging) automation system. GitHub Actions analyzes markets with CCXT + Gemini and executes Kraken spot trades in GBP; a Railway-hosted Discord bot controls repository variables and dispatches workflows. The separate portfolio report remains a legacy Bitkub-only path.

## Architecture

### Module Dependency Graph

```text
discord_bot.py [Railway Docker]
    ├── Discord Gateway + Gemini API
    └── GitHub REST API (repo variables + configurable-ref workflow dispatch)

crypto_analysis.py [GitHub Actions]
    ├── CCXT / Binance US + Gemini API
    ├── Discord webhook
    └── DCA_TARGET_MAP TIME output

crypto_dca.py [GitHub Actions]
    ├── kraken_client.py → CCXT / Kraken COIN/GBP
    ├── bitkub_client.py → THB→USD logging FX + optional legacy Bitkub path
    ├── portfolio_logger.py → Ghostfolio
    ├── gist_logger.py → GitHub Gist
    └── GitHub API → DCA_TARGET_MAP.LAST_BUY_DATE

portfolio_balance.py
    └── bitkub_client.py → legacy Bitkub-only balances and history
```

### Data Flow

- `DCA_TARGET_MAP` (GitHub repo variable) is the central config: `{"BTC_THB": {"TIME": "23:00", "AMOUNT": 800, "BUY_ENABLED": true, "LAST_BUY_DATE": ""}}`
- `crypto_analysis.py` updates `TIME` fields via GitHub Actions output → workflow merge step
- `crypto_dca.py` reads the map, executes trades, then updates `LAST_BUY_DATE` via GitHub API with 3-retry logic
- `discord_bot.py` reads/writes `DCA_TARGET_MAP` directly via GitHub API; when `DCA_CRON_ENABLED=true`, it schedules `daily_dca.yml` dispatches from -30 min to +60 min of each target TIME. Dispatches use `GITHUB_WORKFLOW_REF` (default `main`); `buy_now` sets TIME to the current HH:MM so the window triggers immediately.

### Key Patterns

- **Symbol conversion**: compatibility config keys map to USDT pairs for analysis (`BTC_THB` → `BTC/USDT`) and Kraken GBP pairs for execution (`BTC_THB` → `BTC/GBP`)
- **Budget conversion**: `DCA_TARGET_MAP.AMOUNT` remains THB-denominated; `kraken_client.py` converts it to GBP immediately before the order and validates Kraken's live minimums
- **Non-blocking secondary ops**: Trade execution is the critical path; Ghostfolio/Gist/Discord logging must never crash a trade (wrap in `try/except Exception`)
- **Double-buy prevention**: Multi-layer — GitHub Actions concurrency groups, bash quick-check, Python `LAST_BUY_DATE` check, post-trade date update with retry
- **GHA masking**: `_gha_mask()` redacts sensitive values (amounts, order IDs) in GitHub Actions logs
- **Timezone**: All local time ops use `TIMEZONE` env var (default `Asia/Bangkok`); Ghostfolio requires UTC conversion
- **FX rates**: `kraken_client.get_thb_quote_rate()` raises on total THB→GBP failure so no order is placed; `get_thb_usd_rate()` returns `0.0` on total failure and is used only for normalized USD logging/reporting

## Workflows (`.github/workflows/`)

| Workflow | Trigger | Python Script | Dependencies |
|---|---|---|---|
| `crypto_analysis.yml` | Daily 21:00 UTC (04:00 BKK) + manual dispatch | `crypto_analysis.py` | `requirements.txt` (ccxt, pandas, google-generativeai, requests) |
| `daily_dca.yml` | Daily 22:00 UTC (05:00 BKK) + manual dispatch | `crypto_dca.py` | `requirements.txt` |
| `portfolio_check.yml` | Push to main + monthly 5th + manual | `portfolio_balance.py` (legacy Bitkub only) | `requests` only (no requirements.txt) |

The analysis workflow has a post-step that merges only `TIME` updates into the live `DCA_TARGET_MAP` (preserving `LAST_BUY_DATE` and other fields).

## Development Commands

```bash
# Syntax check any file
python -m py_compile crypto_dca.py

# Install dependencies (GitHub Actions scripts)
pip install -r requirements.txt

# Install dependencies (Discord bot)
pip install -r bot_requirements.txt

# Run Discord bot locally (requires env vars from .env)
python discord_bot.py

# Docker (Discord bot only)
docker compose up -d --build
docker compose logs -f
```

## Python Conventions

- **Target Python 3.12** — use `X | None` not `Optional[X]`, prefer modern syntax
- **f-strings only** — no `%` or `.format()`
- **PEP 8**: snake_case for functions/variables, UPPER_CASE for module constants
- **Imports**: stdlib → third-party → local modules. Use `kraken_client` for Kraken orders and `bitkub_client` only for the legacy Bitkub path and shared THB/USD helpers.
- **Error handling**: Never bare `except:` — use `except Exception as e:` minimum. CCXT/Kraken failures raise exceptions; legacy Bitkub responses use `{"error": 0}` on success.
- **Retry pattern**: State-updating operations (e.g., `save_last_buy_date`) use 3 retries with exponential backoff, fail loudly on exhaustion
- **Discord embeds**: Green `0x00C851` for success, red `0xFF4444` for errors, blue `0x33B5E5` / `3447003` for informational

## Environment

All secrets/config are injected via GitHub Actions or protected Railway variables — never hardcoded. The Railway bot uses `DISCORD_BOT_TOKEN`, `GEMINI_API_KEY`, `GH_PAT`, `GITHUB_REPO`, and `GITHUB_WORKFLOW_REF` plus its channel/scheduler settings. See README.md for the full table.

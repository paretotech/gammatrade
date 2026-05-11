# gammatrade

A self-hosted options-trading dashboard with broker-CSV import, calendar-style P&L tracking, MFE/MAE analytics, and Claude-powered pregame analysis. Runs as a local FastAPI webapp (`http://localhost:8765`) — your trade data never leaves your machine.

Distributed as a [Claude Code](https://claude.com/claude-code) plugin: slash commands invoke trade-decision workflows from inside your existing Claude Code session, so you don't need an Anthropic API key.

## Install

```bash
# 1. Add this repo as a Claude Code plugin marketplace
claude plugin marketplace add paretotech/gammatrade

# 2. Install the plugin
claude plugin install gammatrade@gammatrade

# 3. Run the setup script (installs Python deps, initializes SQLite,
#    loads the bundled reference cohort). Claude Code does not run
#    npm-style post-install hooks, so this step is manual.
bash "$(claude plugin path gammatrade)/hooks/post-install.sh"
```

If `claude plugin path` is unavailable on your CLI version, the plugin
is installed under `~/.claude/plugins/cache/gammatrade/gammatrade/<version>/`.

The setup script will:
1. Install Python dependencies (`fastapi`, `uvicorn`, `pandas`, etc.)
2. Initialize a local SQLite at `~/.gamma/automation/state.db`
3. Load the bundled anonymized reference cohort (742 closed trades)
4. Print the URL: open `http://localhost:8765`

## Usage

### Inside Claude Code

| Slash command | What it does |
|---|---|
| `/gamma-start` | Start the local dashboard server |
| `/gamma-stop` | Stop it cleanly |
| `/gamma-import-trades <path>` | Import a TD Ameritrade or IBKR CSV (auto-detects format) |
| `/gamma-pregame [path]` | Analyze tonight's pregame plan against historical data |
| `/gamma-trigger <TICKER>` | Fast live setup check — reference cohort + your history |
| `/gamma-eod` | End-of-day review of today's closed trades |

### In the browser

Open `http://localhost:8765` and click around:

- **Dashboard** — today's positions, open chains, daily metrics
- **Pregame** — paste your night-before plan; results from the `pregame-analysis` skill render here
- **New Entry** — manual trade entry with TP ladder builder + roll plan + risk gates
- **Triggers** — conditional entries (fires on level break + AND-conditions)
- **Analytics** — calendar heat-map P&L, trade log with expandable detail, MFE/MAE per ticker, time-to-TP, weekly/monthly aggregates
- **Settings** — rules YAML, risk caps (per-regime trade limits, daily $ loss, sector concentration, blackout windows with event calendar), broker config, kill switch

## Architecture

```
┌─ Claude Code session ─────────────────────────────┐
│   slash commands, skills, your terminal           │
│                                                   │
│   /gamma-import-trades  → bash + python scripts   │
│   /gamma-pregame        → reads SQLite, generates │
│                            analysis using YOUR    │
│                            Claude Code session    │
│                            (no API key needed)    │
└────────────────────┬──────────────────────────────┘
                     │ both read/write the same SQLite
                     ▼
┌─ Local FastAPI server (localhost:8765) ──────────┐
│   webapp UI                                       │
│   - dashboard, analytics, trade log               │
│   - broker CSV import button                      │
│   - settings (risk limits, rules, broker)         │
└────────────────────┬──────────────────────────────┘
                     │
                     ▼
            ~/.gamma/automation/state.db
            (your trades, fills, orders, intents)
```

## Broker support

**Import formats:**
- TD Ameritrade `*-orderStatus-*.csv` (daily / historical)
- Interactive Brokers `*.TRANSACTIONS.*.csv` (Flex Web Reports)

Detection is automatic per file. Both normalize to the same canonical symbol shape so cross-broker dedupe works.

**Live trading (optional):** the engine has IBKR Gateway integration via `ib_insync`. Set `BROKER=ibkr_paper` (or `ibkr_live`) before starting the server. Read-only mode is the default and must be explicitly disabled.

## Reference cohort

The plugin ships with `data/reference/reference_cohort.csv` — 742 anonymized closed trades used as a baseline for analytics. Trades are tagged `reference_trader_01`; entry IDs are one-way hashed; narrative columns are stripped; timestamps rounded to 5-min boundaries. The cohort lives in a separate `reference_trades` table so it never mixes with your own trades.

## Data privacy

Everything runs on your machine. The SQLite store at `~/.gamma/automation/state.db` is the only persistent state — no cloud sync, no analytics tracking, nothing transmitted. The plugin doesn't require an API key.

## Stack

- Python 3.11+ · FastAPI · Jinja2 · Tailwind (CDN) · htmx · Alpine.js
- SQLite (single-file local store)
- pandas + pyarrow for CSV / parquet
- optional: `anthropic` SDK (fallback path for the analysis module if you want to call the API directly instead of using the Claude Code skill)
- optional: `ib_insync` for live trading via IBKR Gateway

## License

MIT

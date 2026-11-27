# Kalshi AI Trading Bot

<div align="center">

[![CI](https://github.com/ryanfrigo/kalshi-ai-trading-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/ryanfrigo/kalshi-ai-trading-bot?style=flat&color=yellow)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/ryanfrigo/kalshi-ai-trading-bot?style=flat&color=blue)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/network)
[![GitHub Issues](https://img.shields.io/github/issues/ryanfrigo/kalshi-ai-trading-bot)](https://github.com/ryanfrigo/kalshi-ai-trading-bot/issues)

**A toolkit for building automated trading strategies on [Kalshi](https://kalshi.com) prediction markets.**

Signed Kalshi API client, market-data ingestion, position tracking, SQLite telemetry, a Streamlit dashboard, and a pluggable LLM client (any model on OpenRouter). Three example strategies ship with the repo as starting points — fork them, replace them, or write your own from scratch.

[Quick Start](#quick-start) · [What's Included](#whats-included) · [Example Strategies](#example-strategies) · [Configuration](#configuration) · [Contributing](CONTRIBUTING.md) · [Kalshi API Docs](https://trading-api.readme.io/reference/getting-started)

</div>

---

> **Read this before running with real money.** No strategy in this repo is guaranteed to make money. The examples lose money on certain markets. Trading prediction markets is hard, the edges are small, and what worked last quarter may not work this quarter. This is a toolkit, not a turnkey bot. Read the code, understand what it does, and tune it for the markets you care about. The authors are not responsible for losses you incur using this software.

---

## Quick Start

```bash
# 1. Clone and set up
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot
python setup_env.py    # creates .venv, installs deps

# 2. Add your API keys
cp env.template .env
# then open .env and fill in KALSHI_API_KEY and OPENROUTER_API_KEY

# 3. Verify connectivity
python cli.py health

# 4. Run an example strategy in paper mode
python cli.py run --paper                  # AI directional (LLM-driven)
python cli.py run --safe-compounder        # Edge-based NO-side, no LLM
```

Open the dashboard in another terminal:

```bash
python cli.py dashboard
```

> **Need API keys?**
> - Kalshi key + private key → [kalshi.com/account/settings](https://kalshi.com/account/settings)
> - OpenRouter key → [openrouter.ai](https://openrouter.ai/)

---

## Docker (tennis bot, always-on)

Keeps the tennis loop running in a container (`restart: unless-stopped`). The **host still has to stay awake** — sleep on the PC pauses Docker Desktop too. For overnight trading, run this on a VPS.

```bash
# 1. Env + Kalshi RSA key in the project root
copy env.template .env          # Windows
# cp env.template .env          # macOS / Linux
# fill in KALSHI_API_KEY, BOT_LIVE=true, BOT_INTERVAL=30

# Private key: kalshi_private_key.pem or kalshi_private_key (no extension)
# in the project root. Or set KALSHI_PRIVATE_KEY to the PEM body instead.

# 2. Build and run in the background
docker compose up -d --build

# 3. Follow logs
docker compose logs -f tennis
```

Stop with `docker compose down`. Override flags in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `KALSHI_API_KEY` | — | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | auto | Whichever of `kalshi_private_key.pem` / `kalshi_private_key` exists in the project root |
| `KALSHI_PRIVATE_KEY` | — | Optional PEM body (no key file needed) |
| `BOT_STRATEGY` | `tennis` | `tennis`, `safe-compounder`, or `directional` |
| `BOT_LIVE` | `true` | `true` places real orders |
| `BOT_LOOP` | `true` | Keep cycling |
| `BOT_INTERVAL` | `30` | Seconds between cycles. Fractions and `0` are allowed; the loop still waits at least 1s, and backs off after errors, HTTP 429s, or SofaScore blocks |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Logs are written to stdout and, unless `ENABLE_FILE_LOGGING=false`, to `./logs` on the host.

---

## Railway (worker, no public port)

The same image runs as a Railway worker. It does not listen on a port (`railway.json` sets the Dockerfile builder and `restartPolicyType: ALWAYS`, and the image does not `EXPOSE` one). There is no health check. Configuration is environment variables only — do not bake a `.env` or a private key into the image.

Set these on the service:

| Variable | Required | Meaning |
|---|---|---|
| `KALSHI_API_KEY` | unless using dashboard accounts | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY` | unless using dashboard accounts | Full PEM. Real newlines or a single line with `\n` both work; a surrounding pair of quotes is stripped |
| `BOT_STRATEGY` | no | `tennis` (default), `safe-compounder`, or `directional` |
| `BOT_LIVE` | no | `true` places real orders |
| `BOT_LOOP` | no | `true` (default) keeps cycling |
| `BOT_INTERVAL` | no | Seconds between cycles. `0` or `0.5` are valid. The process still pauses at least 1 second, and after a failed cycle, a Kalshi 429, or a SofaScore block/403 the wait doubles up to 60 seconds |
| `LOG_LEVEL` | no | `INFO` |
| `ENABLE_FILE_LOGGING` | no | Set `false` on Railway. Logs always go to stdout, which Railway collects |
| `KALSHI_BASE_URL` | no | Leave unset unless you mean to override the production host |

`KALSHI_PRIVATE_KEY_PATH` is unnecessary when `KALSHI_PRIVATE_KEY` is set. Do not commit the PEM. To use only accounts pasted in the dashboard, leave both key variables empty and set `KEYS_ENCRYPTION_KEY` and `DATABASE_URL` on this service as well as on the dashboard.

### Several accounts

One deployment can run the tennis strategy on more than one Kalshi account. Each account has its own client, bankroll, position cap, and journal tag. SofaScore is fetched once per cycle and shared. If one account errors, the others still run.

Set `KALSHI_ACCOUNTS` to a JSON list:

```json
[{"name":"main","api_key_id":"KEY","private_key":"<PEM body; use \\n between lines>"}]
```

Or use numbered variables, starting at 1 with no gaps: `KALSHI_ACCOUNT_NAME_1`, `KALSHI_API_KEY_ID_1`, `KALSHI_PRIVATE_KEY_1`, then `_2`, and so on. `KALSHI_API_KEY_1` is an alias for the key id. If neither form is set, the single `KALSHI_API_KEY` / `KALSHI_PRIVATE_KEY` pair is account `default`.

Accounts can also be pasted on the dashboard Accounts page. Those are stored encrypted in Postgres and loaded, if enabled, on every cycle in addition to the environment accounts. See the dashboard section below. A stored account whose name matches an environment account is skipped so the same key is not traded twice.

### Journal, report, dashboard

Every entry decision, order, stop, take-profit, and settlement is appended to a journal, tagged with the account name. `DATABASE_URL` starting with `postgres://` or `postgresql://` (what Railway injects for its Postgres) selects Postgres. A `sqlite://` URL uses SQLite. `TRADE_LOG_BACKEND=jsonl` writes `TRADE_LOG_PATH` (default `tennis_journal.jsonl`) instead.

```bash
python cli.py tennis-report                  # win rate, avg entry, PnL by rule, implied edge
python cli.py tennis-report --account main
DASHBOARD_PASSWORD=choose-one python cli.py tennis-dashboard
```

The settled edge is the realized win rate on contracts held to settlement minus the average entry price. Positive means those entries won more often than the price implied.

### Dashboard service on Railway

Add a second service in the same Railway project from this repo. Leave the trading service as a worker (`BOT_MODE` unset). On the new service:

| Variable | Required | Meaning |
|---|---|---|
| `BOT_MODE` | yes | `dashboard`. The image entrypoint then runs `python -m src.journal.web` and does not look for a Kalshi key |
| `DATABASE_URL` | yes | The same Postgres URL the worker uses. Add it as a variable reference to the Postgres plugin |
| `DASHBOARD_PASSWORD` | yes | Login password. The process refuses to serve the journal or accounts when this is empty |
| `KEYS_ENCRYPTION_KEY` | for Accounts | Fernet key. Required on this service and on the worker, with the same value. Without it, key storage does not start |
| `DASHBOARD_COOKIE_SECURE` | no | Default `true` (Secure session cookie). The local `tennis-dashboard` command turns it off for localhost HTTP |
| `DASHBOARD_PUBLIC_URL` | no | Public origin, for example `https://your-dashboard.up.railway.app`. Always accepted as the form Origin when Railway's `Host` header is the internal upstream |
| `PORT` | automatic | Railway sets this. The server binds `0.0.0.0:$PORT` |

Generate the encryption key locally and paste it into Railway. Do not commit it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Give that service a public domain. Optional health check path: `/healthz` (no journal data, no password). Do not set `BOT_MODE=dashboard` on the trading service.

If the service start command replaces the image entrypoint entirely, set it to `python -m src.journal.web`. If Railway passes the start command through the entrypoint, `dashboard` is enough. The journal page shows open positions, recent decisions and trades, win rate, cumulative PnL (Chart.js), PnL by entry rule, and the last cycle time for each account. It cannot place orders.

Login is a password form. The session cookie is HttpOnly, SameSite=Strict, and Secure in production. Every form includes a CSRF token. After 8 failed logins from the same address within 10 minutes, further attempts are rejected for that window. Behind Railway, the browser Origin is the public URL and `Host` is often the private upstream. The login check also accepts `X-Forwarded-Proto` plus the first `X-Forwarded-Host`, and `DASHBOARD_PUBLIC_URL` when you set it. The process trusts those forwarded headers (`forwarded_allow_ips=*`) because the edge proxy is the only public path. A mismatch is logged at warning with the Origin, Host, and X-Forwarded-Host, and not the password or cookie.

The Accounts page (same login) accepts a name, an API key ID, and a private key PEM. The private key is encrypted with `KEYS_ENCRYPTION_KEY` and written to Postgres. The page then shows the key ID and the last four characters of the key body, never the PEM, and the process does not log the key. Buttons on that page enable or disable the account, switch it between dry run and live, test the connection (a read-only balance call), and delete it. New accounts default to dry run. A stored account places real orders only when its toggle is Live and the worker is running with `BOT_LIVE=true`. The worker reads enabled stored accounts on every cycle, so saving a key does not require a redeploy. Set `KEYS_ENCRYPTION_KEY` and `DATABASE_URL` on the worker too. With both set, the worker can start with no `KALSHI_PRIVATE_KEY`; it uses the enabled dashboard accounts. If the encryption key is missing, the worker keeps trading the environment accounts and skips the database keys. A stored account whose name matches an environment account is skipped.

Semifinals and finals are not entered by default (`SKIP_ROUNDS=semifinal,final`). That is semifinal and above: semifinals, the final, and a championship or title match. The round comes from SofaScore `roundInfo` (`name`, `slug`, `cupRoundType` 1 for a final and 2 for a semifinal) or, if that is missing, from the Kalshi event title. `Semifinal`, `Semi-final`, `SF`, `semifinals`, `Final`, and `Championship` all count. `Quarterfinal` and `Quarter-final` do not: the word final inside them is not the final. Set `SKIP_ROUNDS=none` to turn the filter off. If neither source identifies a round, the bot logs that the round is unknown and allows the entry. A tournament named "ATP Finals" is not treated as the final round.

---

## What's Included

This repo gives you the building blocks. The example strategies use them — your own strategies can too.

| Component | What it does | Where it lives |
|---|---|---|
| **Kalshi client** | Authenticated REST + WebSocket client (RSA signing, retries, rate-limit handling) | `src/clients/kalshi_client.py` |
| **Market ingestion** | Pulls the full tradeable universe via the Events API, persists to SQLite | `src/jobs/ingest.py` |
| **Position tracking** | Stop-loss, take-profit, time-based, and resolution-based exits with real Kalshi sell orders | `src/jobs/track.py` |
| **LLM client** | Single OpenRouter API key, swap models with one config line, fallback chain on errors, persistent daily-cost tracker | `src/clients/openrouter_client.py`, `src/clients/xai_client.py` |
| **SQLite telemetry** | Every trade, AI decision, and cost metric logged locally | `src/utils/database.py` |
| **Streamlit dashboard** | Real-time portfolio, positions, P&L, decision logs | `beast_mode_dashboard.py` |
| **Paper trading** | Log signals against settled markets without sending orders | `paper_trader.py` |
| **CLI** | `run`, `dashboard`, `status`, `health`, `scores`, `history`, `close-all` | `cli.py` |
| **Risk helpers** | Kelly sizing, stop-loss math, drawdown circuit breaker | `src/utils/`, `src/strategies/` |

The repo also ships scaffolding for things that aren't fully wired — multi-agent debate runners in `src/agents/`, sentiment analyzer in `src/data/`, etc. Treat them as starting points if you want to extend them.

---

## Example Strategies

Three strategies ship with the repo. **None of them is "the right answer."** They exist so you can run something end-to-end and see how the pieces connect, then fork the one closest to what you want to build.

### 1. AI Directional — `python cli.py run`

The default. For each candidate market, it calls a single LLM via OpenRouter (with a fallback chain on errors) to score directional confidence, then sizes positions with fractional Kelly and applies category/sector guardrails.

> **It is not a "5-model ensemble"** despite earlier README claims. One model is called per decision. The fallback chain only triggers on errors. The agents/ directory contains scaffolding for real parallel multi-model voting, but it's not wired into the live trading path. If you want a real ensemble, fork `src/jobs/decide.py` and build it.

```bash
python cli.py run --paper          # paper trading
python cli.py run --live           # live trading (real money)
```

Defaults: 15% max drawdown, 45% min confidence, 3% max position size, 30% max sector concentration, quarter-Kelly. All configurable in `src/config/settings.py`.

### 2. Safe Compounder — `python cli.py run --safe-compounder`

Pure edge-based math, no LLM required. Scans every active Kalshi market for NO-side asks above a price threshold with a positive expected-value edge, then places resting maker orders one cent below the ask.

```bash
python cli.py run --safe-compounder              # dry-run preview
python cli.py run --safe-compounder --live       # live execution

# Run continuously instead of one cycle and exit:
python cli.py run --safe-compounder --live --loop --interval 300
```

Rules: NO side only, YES last ≤ 20¢, NO ask > 80¢, edge > 5¢, max 10%/position, skips sports/entertainment/"mention" markets.

### 3. Beast Mode — `python cli.py run --beast`

Aggressive settings with no category guardrails. Available for comparison and experimentation — **not recommended for live trading**. Running this with real money historically led to significant losses on this repo.

---

## Stopping Cleanly

Ctrl-C sends SIGINT and triggers graceful shutdown — the bot finishes the in-flight cycle, logs, and exits. Open positions remain on Kalshi until they resolve.

If you want to **liquidate everything** before stepping away:

```bash
# 1. Stop the bot
Ctrl-C

# 2. Place limit sells at the current best bid for every open position
python cli.py close-all              # dry-run preview
python cli.py close-all --live       # actually send orders

# 3. Verify
python cli.py status
```

`close-all` queries Kalshi directly (not the local DB), so it works even when local state is stale. Sells are limit-priced, so they may rest unfilled on thin books — check Kalshi or `cli.py status` after a minute.

---

## Installation

### Prerequisites

- Python 3.12 or later
- A [Kalshi](https://kalshi.com) account with API access ([API docs](https://trading-api.readme.io/reference/getting-started))
- An [OpenRouter](https://openrouter.ai/) API key (only needed for the AI directional strategy)

### Automated Setup

```bash
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot
python setup_env.py
```

Creates a virtual env, installs dependencies, and prints next steps.

### Manual Setup

```bash
git clone https://github.com/ryanfrigo/kalshi-ai-trading-bot.git
cd kalshi-ai-trading-bot

python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate          # Windows

pip install -e .
```

### Configuration

```bash
cp env.template .env
# then edit .env with your keys
```

| Variable | Description |
|---|---|
| `KALSHI_API_KEY` | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the RSA PEM (default `kalshi_private_key.pem`) |
| `KALSHI_PRIVATE_KEY` | Optional PEM body; if set, used instead of the file. Escaped `\n`, real newlines, and surrounding quotes are accepted |
| `OPENROUTER_API_KEY` | OpenRouter key (only for AI directional strategy) |
| `BOT_STRATEGY` | Docker: `tennis`, `safe-compounder`, or `directional` |
| `BOT_LIVE` | Docker: `true` to pass `--live` |
| `BOT_LOOP` | Docker: keep looping |
| `BOT_INTERVAL` | Seconds between cycles (fractions and 0 allowed; 1s minimum, backoff on errors) |

Place your Kalshi private key as `kalshi_private_key.pem` in the project root (`kalshi_private_key` with no extension also works). Download it from [Kalshi Settings → API](https://kalshi.com/account/settings). It's git-ignored.

Verify everything is wired:

```bash
python cli.py health
```

---

## Configuration

All trading parameters live in `src/config/settings.py`. The most useful knobs:

```python
# Position sizing
max_position_size_pct  = 3.0     # Max 3% of balance per position
max_positions          = 10      # Max concurrent positions
kelly_fraction         = 0.25    # Quarter-Kelly (conservative)

# Market filtering
min_volume             = 500     # Minimum contract volume
max_time_to_expiry_days = 14     # How far out to trade
min_confidence_to_trade = 0.45   # Minimum AI confidence to enter

# LLM (OpenRouter)
primary_model          = "anthropic/claude-sonnet-4.5"
ai_temperature         = 0       # Deterministic
ai_max_tokens          = 8000

# Risk management
max_daily_loss_pct     = 10.0    # Daily loss circuit breaker
max_drawdown           = 0.15    # Portfolio drawdown halt
daily_ai_cost_limit    = 10.0    # Max daily LLM spend in USD
```

**Swapping models:** change `primary_model` to any slug from [openrouter.ai/models](https://openrouter.ai/models). The fallback chain in `src/clients/openrouter_client.py` controls what happens when the primary errors.

**Controlling LLM spend:** the bot checks the daily limit before every API call and skips trading until the next calendar day once exhausted. Set `DAILY_AI_COST_LIMIT` in `.env` to override.

---

## Project Structure

```
kalshi-ai-trading-bot/
├── beast_mode_bot.py          # Example AI directional bot — main loop orchestration
├── cli.py                     # Unified CLI: run, dashboard, status, health, close-all, scores, history
├── paper_trader.py            # Paper-trading signal logger + static dashboard
├── setup_env.py               # Bootstrap script (interactive env setup — not a setuptools file)
├── env.template               # Environment variable template
│
├── src/
│   ├── agents/                # UNWIRED scaffolding for multi-agent debate (fork to use)
│   ├── clients/               # Kalshi, OpenRouter, WebSocket clients
│   ├── config/                # Settings and trading parameters
│   ├── data/                  # News + sentiment helpers (optional)
│   ├── events/                # Async event bus
│   ├── jobs/                  # ingest, decide, execute, track, evaluate
│   ├── strategies/            # Safe compounder, category scorer, portfolio enforcer
│   └── utils/                 # Database, logging, prompts, risk helpers
│
├── scripts/                   # Diagnostic and utility scripts
├── docs/                      # Additional docs + paper-trading dashboard HTML
└── tests/                     # Pytest suite
```

---

## Paper Trading

Simulate trades without sending real orders. Every signal is logged to SQLite and a static HTML dashboard renders cumulative P&L after markets settle.

```bash
python paper_trader.py                            # one scan
python paper_trader.py --loop --interval 900      # continuous, every 15m
python paper_trader.py --settle                   # update outcomes for resolved markets
python paper_trader.py --dashboard                # regenerate HTML
python paper_trader.py --stats                    # print stats
```

Output goes to `docs/paper_dashboard.html`.

---

## Category Scoring (used by AI Directional)

The category scorer evaluates each Kalshi market category on a 0-100 scale based on historical ROI, win rate, recent trend, and sample size. Allocation per category is gated by score.

| Score | Max Position | Status |
|---|---|---|
| 80–100 | 20% | STRONG |
| 60–79 | 10% | GOOD |
| 40–59 | 5% | WEAK |
| 20–39 | 2% | POOR |
| 0–19 | 0 | BLOCKED |

```bash
python cli.py scores
```

This is one heuristic for category-level risk control. If it doesn't fit your strategy, ignore it — it's only used by the AI directional path.

---

## Performance Tracking

Every trade, AI decision, and cost metric is recorded to `trading_system.db` (local SQLite). Inspect via the dashboard or:

```bash
python cli.py history                # Last 50 trades
python cli.py history --limit 100    # Last 100
python cli.py status                 # Live balance + open positions from Kalshi
```

---

## Development

### Running Tests

```bash
pytest                 # safe suite (no credentials needed; never touches the live API)
pytest -v              # verbose
pytest --cov=src       # with coverage
```

Tests marked `live` hit the real Kalshi API — some **place real orders** — and
are skipped by default. To run them (against an account whose losses you accept):

```bash
RUN_LIVE_TESTS=1 pytest -m live
```

### Code Quality

```bash
black src/ tests/ cli.py beast_mode_bot.py
isort src/ tests/ cli.py beast_mode_bot.py
mypy src/
```

### Adding a New Strategy

1. Create a module under `src/strategies/`
2. Wire it into a CLI flag in `cli.py` (or invoke it directly)
3. Use the `KalshiClient` for orders/positions and `DatabaseManager` for state
4. Add tests under `tests/`

---

## Troubleshooting

<details>
<summary><strong>Health check fails with HTTP 401</strong></summary>

A 401 from Kalshi almost always means one of three things:

1. `KALSHI_API_KEY` in `.env` doesn't match the API key ID shown in Kalshi
2. The private key file (`kalshi_private_key.pem` or `kalshi_private_key`) is the wrong key for that API key, or its path is wrong
3. The key was created on Kalshi's demo environment but you're pointing at production (or vice versa)

Re-download the key pair from Kalshi and verify both values point to the matching pair. The health check will print this hint when it detects a 401.

</details>

<details>
<summary><strong>"Shutdown signal received" without pressing Ctrl-C</strong></summary>

The bot now logs which signal arrived (SIGINT, SIGTERM, or SIGHUP). If you see SIGTERM or SIGHUP without sending it yourself, common causes:

- Parent shell closed (run inside `tmux`, `screen`, or with `nohup`)
- A cloud platform / systemd / launchd timeout
- Another shell sent `kill <pid>`
- An OOM killer warning before SIGKILL

The bot did not kill itself — something external ended the process.

</details>

<details>
<summary><strong>Bot ran for weeks but placed no positions</strong></summary>

This is expected behavior, not a bug. The example strategies are conservative by design:

- **AI Directional** requires confidence ≥ 45%, category score ≥ 30, and is gated by drawdown / sector caps. On many days, no markets clear all four filters.
- **Safe Compounder** requires NO ask > 80¢ AND edge > 5¢. Most NO-side markets don't meet both.

If you want more activity, lower the thresholds in `src/config/settings.py` (or the relevant strategy file) — but that means taking lower-edge bets. Or write your own strategy that targets the markets you actually have an edge on. This repo is a toolkit; the example thresholds are starting points.

</details>

<details>
<summary><strong>"no such table: positions" error on fresh install</strong></summary>

The DB file isn't committed; it's created at runtime. The bot auto-initializes on startup, but you can do it manually:

```bash
python -m src.utils.database
```

Use `-m` — running `python src/utils/database.py` directly fails with an import error.

</details>

<details>
<summary><strong>AdGuard (macOS) blocks dependency downloads</strong></summary>

If AdGuard is running as a system-level proxy, `pip install` may time out during setup. Disable AdGuard at the system level for the install, then re-enable it. AdGuard as a browser extension is fine.

</details>

<details>
<summary><strong>Bot not placing live trades despite --live</strong></summary>

```bash
grep -i "live trading\|paper trading\|LIVE ORDER" logs/trading_system.log | tail -20
```

If you see "Paper trading mode" the flag isn't taking effect. Verify the API key has trading permissions in [Kalshi Settings](https://kalshi.com/account/settings).

</details>

<details>
<summary><strong>Model not found / OpenRouter API errors</strong></summary>

Model names on OpenRouter change. Update `primary_model` in `src/config/settings.py` with a current slug from [openrouter.ai/models](https://openrouter.ai/models), or set `PRIMARY_MODEL` in `.env`.

</details>

<details>
<summary><strong>Bot only seeing "KXMVE" tickers</strong></summary>

The Kalshi `/markets` endpoint returns parlay tickers; real markets live under the Events API. The ingestion pipeline already uses Events with nested markets. If you see only `KXMVE*`, check API permissions and run `python cli.py health`.

</details>

<details>
<summary><strong>Python 3.14 PyO3 compatibility error</strong></summary>

```bash
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
pip install -e .
```

Or use Python 3.13:

```bash
pyenv install 3.13.1 && pyenv local 3.13.1
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

</details>

---

## Lessons From Live Trading

These are observations from running the example strategies with real money on Kalshi. They informed the defaults shipped here. They are **not** universal trading wisdom — they're notes from one set of experiments.

**1. Category discipline mattered more than AI confidence.** The LLM could be 80% confident on a CPI trade and still be wrong. Market-implied probabilities on highly-watched economic releases are already efficient.

**2. Kelly fraction matters enormously.** Three-quarter Kelly compounds losses catastrophically on a 45% win-rate strategy. Quarter-Kelly is what the example strategies use.

**3. A 50% drawdown limit isn't a limit.** The default is 15% with the circuit breaker actually halting trades, not just logging.

**4. Sector concentration creates correlated losses.** When 90% of capital is in economic categories on a Fed day, everything moves together. The default cap is 30% per category.

**5. More trades without edge is faster path to zero.** The default scan interval is 60 seconds, not 30, and trades are gated by both confidence and category score.

Your edge is probably somewhere else. Use the toolkit to find it.

---

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

```bash
# 1. Fork
# 2. Create a feature branch
git checkout -b feature/your-feature
# 3. Make changes, add tests, run pytest and black
# 4. Commit using conventional commits (feat:, fix:, refactor:)
# 5. Open a PR
```

---

## Resources

- [Kalshi Trading API](https://trading-api.readme.io/reference/getting-started)
- [Kalshi API Authentication](https://trading-api.readme.io/reference/authentication)
- [Kalshi Markets](https://kalshi.com/markets)
- [OpenRouter Model Catalog](https://openrouter.ai/models)

---

## License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

**Found this useful?** ⭐ [Star the repo](https://github.com/ryanfrigo/kalshi-ai-trading-bot) so others can find it · 💬 [Ask or share in Discussions](https://github.com/ryanfrigo/kalshi-ai-trading-bot/discussions) · ❤️ [Sponsor the work](https://github.com/sponsors/ryanfrigo)

</div>

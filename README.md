# Configurable Crypto Trading Bot

This repository is initialized from the quant infrastructure blueprint and provides a minimal local-first trading engine skeleton.

## Project structure

- `engine.py` – core trading engine and local dashboard
- `config/` – declarative strategy profiles
- `backtest.py`, `indicators.py`, `rules.py`, `model_config.py`, `friction.py`, `chart.py` – simulation engine, indicator library, rule language, profile validation, friction model and charts
- `tests/` – verification tests

## Runtime modes

This project now separates the runtime into two distinct tools:

- `engine.py` — production headless engine for live node execution
- `simulator.py` — simulation and strategy testing tool with a GUI interface for visual feedback

## Private configuration policy

The repository intentionally ignores the local `config/` directory so strategy models and runtime settings stay private. Public templates live in `config/.example/`, which is tracked; real strategy files must live only in the local, untracked configuration folder.

## Model profiles

A profile names its indicators (moving averages, RSI, MACD, Bollinger Bands, ATR, ADX, VWAP and about 20 more) and writes its entry and exit signals as formulas over them:

```json
"indicators": {
  "fast_ema": {"type": "EMA", "period": "12h"},
  "slow_ema": {"type": "EMA", "period": "3d"},
  "rsi": {"type": "RSI", "period": 14}
},
"strategy": {
  "entry_signal": {"all": ["crosses_above(fast_ema, slow_ema)", "rsi < 70"]},
  "exit_signal": {"any": ["crosses_below(fast_ema, slow_ema)"], "profit_lock_target": 0.08}
}
```

- [Project_Documentation/indicator_reference.md](Project_Documentation/indicator_reference.md) documents every indicator, the rule language, strategy recipes and troubleshooting.
- [Project_Documentation/config_file_structure.md](Project_Documentation/config_file_structure.md) covers the rest of a profile: friction, capital allocation, execution and safeguards.

## Production engine

The production engine remains the headless runtime for the local node. It loads `.env` settings and uses them for exchange connectivity and run configuration.

## Simulator

The simulator is a separate tool intended for configuration testing and visual inspection. It exposes a small GUI window and accepts a JSON strategy profile for rapid evaluation.

Run it with:

```bash
python simulator.py
```

The **Models** list shows every profile in `config/`. Select one, or several with
Ctrl/Shift-click (or **Select all**), to test them in the same run; **Add file...**
adds profiles from elsewhere and **Refresh** picks up new files.

**Start testing from** (optional, UTC, such as `2020-01-01`) starts the test at a point
in time of your choosing. Each model holds its starting capital in cash from that moment
and waits for its own entry rules before buying; the history before the date still warms
up the indicators, so they are fully formed on day one. Returns, buy and hold, drawdown,
taxes and the charts are all measured from the start date. Tick **Wait for a fresh entry
signal** to skip a trend that is already underway on the start date and wait for the
signal to switch off and fire again, so a start near a market top doesn't buy the top.
Leave the date blank to test all 10 years. The Metrics tab reports the start, the warm-up
and how long each model waited for its first entry.

**Load Profile** validates the selected profiles and lists each one's starting value,
indicators and signal rules, or every problem it found. **Run Simulation** backtests
every selected model over the last 10 years of candles for its `product_id` and
`period_granularity` (or as much history as the product has). Progress bars track
each phase:

- **API check** confirms the Coinbase API key works and looks up the live fee rates,
  once for the whole run.
- **Data gathering** downloads candles from Coinbase and caches them in `data/`.
  Models on the same product and candle size share one download, and later runs load
  the cache and fetch only candles that closed since the last run.
- **Simulation** runs each model in turn: its indicators, then its long-only entry and
  exit rules over every candle (see `backtest.py` for the fill and fee assumptions).

The results tabs:

- **Comparison** ranks every model in the run by return, with its ending value, maximum
  drawdown, trades, win rate, fees and taxes. Double-click a model to open its chart.
- **Chart** plots the chosen model's close price, indicators (oscillators in their own
  panels) and each buy and sell on a log price scale; use the toolbar to zoom and pan,
  and hover for exact values.
- **Model value** draws every model's value over time on one chart, against buying and
  holding the same starting capital.
- **Trades** lists the chosen model's trades with prices, holding time, P&L, return,
  outcome and exit reason; click a heading to sort, or double-click a trade to show it
  on the chart.
- **Metrics** summarizes the chosen model's returns, drawdown, friction, taxes and
  recent trades.

**Showing model**, above the tabs, picks which model the Chart, Trades and Metrics tabs
show; after a run it starts on the best performer.

## Environment configuration

Create a local `.env` file to store API keys and runtime secrets. The repository ignores this file so credentials stay off version control.

Use the template in `.env.example` and copy it to `.env` before running the engine.

```bash
cp .env.example .env
```

Example values:

```env
COINBASE_API_KEY_NAME=organizations/.../apiKeys/...
COINBASE_API_KEY_SECRET=your_cdp_secret
COINBASE_REQUIRED_PERMISSIONS=view
COINBASE_ALLOW_TRADING=false
COINBASE_BASE_URL=https://api.coinbase.com
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret
KRAKEN_BASE_URL=https://api.kraken.com
LAN_PORT=8080
LIVE_EXECUTION_ACTIVE=false
```

Coinbase access uses the official `coinbase-advanced-py` SDK with a CDP Secret
API key (Ed25519 or ECDSA). Instead of the key name and secret, you can set
`COINBASE_KEY_FILE` to the key JSON downloaded from the Coinbase Developer
Platform; relative paths resolve from the engine's base directory.

`COINBASE_REQUIRED_PERMISSIONS` documents the permissions the application
expects the Coinbase key to have; it does not grant permissions. Keep both
`COINBASE_ALLOW_TRADING` and `LIVE_EXECUTION_ACTIVE` false until trading is
deliberately enabled.

Start the local engine and call `http://localhost:8080/api/coinbase/authenticate`
or use the simulator's **Test Coinbase Authentication** button. The engine
lists accounts through the SDK's `RESTClient`, which signs each request with a
short-lived JWT, and logs the result without printing the key or secret.

The simulator uses the same authentication configuration and displays the full
authentication result in its metrics panel.

Before authentication, both tools check that `coinbase-advanced-py` is installed and that
the active Python directory is on `PATH`. Missing packages and PATH updates
require approval. On Windows, an approved PATH update is persisted for the
current user and applied to the current process immediately. PATH is not needed
for imports when the correct virtual-environment interpreter is already used.

## Local validation

```bash
python -m pytest
```

## Notes

The architecture intentionally keeps the production environment local, uses SQLite for low-wear storage, and isolates the monitoring endpoint to the private LAN. The simulator operates independently to provide visual feedback without affecting live trading execution.

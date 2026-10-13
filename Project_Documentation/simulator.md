# Simulator Guide

The simulator (`simulator.py`) is a desktop app for testing [model profiles](model_configuration.md) against historical Coinbase data. A run works in three steps:

1. It downloads up to 10 years of candles.
2. It replays each selected strategy over those candles, charging fees, slippage and taxes.
3. It shows the results as tables and interactive charts.

The simulator only **reads** from Coinbase. It never places an order.

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Launching the simulator](#2-launching-the-simulator)
3. [Your first run](#3-your-first-run)
4. [The simulator window](#4-the-simulator-window)
5. [What happens during a run](#5-what-happens-during-a-run)
6. [Historical data and the cache](#6-historical-data-and-the-cache)
7. [Reading the results](#7-reading-the-results)
8. [How trades are simulated](#8-how-trades-are-simulated)
9. [Scripting a backtest without the window](#9-scripting-a-backtest-without-the-window)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Before you start

You need three things.

1. **Python and the project's packages.** From the repository root, install them into a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows; on macOS or Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

   The window uses tkinter. The python.org installers for Windows and macOS include it. On Debian or Ubuntu, install it with `sudo apt install python3-tk`.

2. **A Coinbase API key in `.env`.** The simulator needs a key even though it never trades, because the candles and your fee tier come from the authenticated Advanced Trade API. A key with only the **View** permission is enough, and is the safest choice. For setup, see [Creating a Coinbase API key](trader.md#3-creating-a-coinbase-api-key) and [Setting up the .env file](trader.md#2-setting-up-the-env-file).

3. **At least one model profile in `config/`.** To start from a template:

   ```bash
   cp config/.example/btc_macro_horizon.example.json config/btc_macro_horizon.json
   ```

---

## 2. Launching the simulator

From the repository root, with the virtual environment active:

```bash
python simulator.py
```

**Run it from the repository root.** The simulator finds these relative to the current directory:

- `.env`
- a relative `COINBASE_KEY_FILE` path
- the `ledger/` folder

The model list (`config/`) and the data cache (`data/`) are always found next to `simulator.py`.

The simulator reuses the trader engine's authentication code. As a side effect, it creates an empty `ledger/fortress.db` in the current directory.

---

## 3. Your first run

1. **Select a model** in the **Models** list.
2. Click **Load Profile**. The **Metrics** tab confirms the profile is valid and lists its starting value, indicators and rules, or it lists every problem it found.
3. *(Optional)* Click **Test Coinbase Authentication** to check your API key on its own.
4. *(Optional)* Type a **Start testing from** date, such as `2022-01-01`.
5. Click **Run Simulation** and watch the three progress bars. The first run for a product and candle size downloads the full history, which can take several minutes. Later runs load it from the cache.
6. Read the results. With one model, the **Chart** tab opens. With several, the **Comparison** tab opens.

---

## 4. The simulator window

From top to bottom:

### Models

The list shows every `.json` file in `config/`, named after the file (`btc_macro_horizon.json` appears as `btc_macro_horizon`).

- Click to select one model. Ctrl-click or Shift-click to select several, or use **Select all**. Every selected model is tested in the same run.
- **Add file...** adds profiles from anywhere on disk to the list.
- **Refresh** picks up files added to or removed from `config/`, and keeps your selection.
- If two files share a name, the second appears as `name (2)`.

### Start testing from

- **The date box** (UTC, optional) starts the test at a moment you choose. It accepts `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` and rejects future dates. Leave it blank to test the full 10 years. See [Start date and warm-up](#start-date-and-warm-up).
- **Wait for a fresh entry signal**: when ticked, a model ignores an entry signal that is already true on the start date. It waits for the signal to switch off and fire again, so a start in the middle of a rally doesn't buy the top.

### Buttons

| Button | What it does |
|---|---|
| **Load Profile** | Validates every selected profile and writes a report to the **Metrics** tab. With one profile selected, the report also shows the profile's JSON. |
| **Run Simulation** | Backtests every selected profile. The button is disabled while a run is in progress. |
| **Test Coinbase Authentication** | Makes a read-only account request with your key and shows the full result in the **Metrics** tab. |

### Progress

| Bar | Tracks |
|---|---|
| **API check** | The authentication test, then the fee lookups. |
| **Data gathering (10 years)** | Candle downloads or cache loads, one product and candle size at a time. |
| **Simulation** | Each model in turn: its indicators, then every candle of its test. |

### Showing model

This drop-down picks which model the **Chart**, **Trades** and **Metrics** tabs show. After a run, it starts on the best performer. The **Comparison** and **Model value** tabs always show every model.

### Status

The bottom of the window shows two status lines: the simulation status (what is running, or the last error), and the result of the last Coinbase authentication.

---

## 5. What happens during a run

When you click **Run Simulation**, the simulator works through these steps:

1. **Validation.** Every selected profile is checked. If any has a problem, nothing runs, and a dialog lists the first problem in each failing profile. **Load Profile** lists every problem.
2. **Client set-up.** The simulator creates a Coinbase client from your `.env` settings. If the Coinbase SDK (`coinbase-advanced-py`) is missing, it asks whether to install it into the running Python. If Python's folder isn't on `PATH`, it offers to add it (see [Troubleshooting](#10-troubleshooting)).
3. **API check.** A read-only request lists your accounts. If it fails, the run stops before downloading anything, and the status line explains why.
4. **Fee lookups**, once for the whole run:
   - **Your live Coinbase fee tier**, if any selected profile uses `commission_rate_source: "exchange_api"`, which is the default.
   - **The Bitcoin network fee** from [mempool.space](https://mempool.space), if any selected profile trades a `BTC-` product.

   If a lookup fails, the run continues with the profile's fallback rate, and the Metrics tab gives the reason.
5. **Data gathering.** Candles are loaded for each distinct product and candle size. Models that share both also share one download.
6. **Simulation.** Each model is backtested in turn.
7. **Results.** The tabs fill in, and the **Showing model** picker selects the best model by return.

There is no cancel button. Closing the window stops a run. A download interrupted that way is not saved, and the next run fetches that range again.

---

## 6. Historical data and the cache

Each run covers the **last 10 years** up to the most recent closed candle, or as much history as the product has. The current, still-open candle is never used. (The 10-year window is `LOOKBACK_YEARS` in `backtest.py`.)

Candles are cached in `data/`, one pair of files per product and candle size:

```text
data/BTC-USD_FIFTEEN_MINUTE.csv         start, open, high, low, close, volume
data/BTC-USD_FIFTEEN_MINUTE.meta.json   the time range already downloaded
```

- **The first run** for a product and candle size downloads everything, in requests of up to 350 candles with a 0.1 second pause between them.
- **Later runs** load the cache and fetch only candles that have closed since the last run.
- **To force a fresh download**, delete both files. If either file is missing, the whole range is downloaded again.
- `data/` is ignored by Git.

The amount of data depends heavily on the candle size:

| `period_granularity` | Candles in 10 years | Requests for a first download |
|---|---|---|
| `ONE_DAY` | about 3,650 | about 11 |
| `SIX_HOUR` | about 14,600 | about 42 |
| `ONE_HOUR` | about 87,700 | about 250 |
| `FIFTEEN_MINUTE` | about 351,000 | about 1,000 |
| `FIVE_MINUTE` | about 1.05 million | about 3,000 |
| `ONE_MINUTE` | about 5.26 million | about 15,000 |

A 15-minute history takes several minutes the first time. One-minute data takes far longer, and it also makes every simulation slower and the charts heavier.

All times are in UTC.

---

## 7. Reading the results

### Comparison

The Comparison tab has one row per model, sorted by return with the best first. Click a column heading to sort by it, and click again to reverse. Double-click a model to open its chart.

| Column | Meaning |
|---|---|
| Model, Product, Candles | The file name, the `product_id` and the candle size. |
| Start, End | The starting capital and the ending value. |
| Return | The ending value divided by the starting capital, minus 1. |
| Buy & hold | The price change over the test period: the last close divided by the first close, minus 1. It includes no fees or taxes. |
| Max drawdown | The largest fall in the model's value from a previous peak. |
| Worst in window | The largest fall from the highest value within the trailing window (`safeguards.drawdown_window`, or 180 days when it is not set). |
| Flash buys | The number of flash-crash limit buys that filled. |
| Trades, Win rate | Completed trades, and the share of them with a positive profit after fees. |
| Fees, Taxes | Total commissions paid, and total tax paid at year boundaries. |
| Blocked signals | Entry signals the [friction screen](model_configuration.md#5-the-friction-screen-worked-through) rejected. |

### Chart

The Chart tab draws the chosen model on a log price scale:

- the close price, with every price-scale indicator (moving averages, VWAP, band and channel lines)
- a separate panel under the price for each oscillator (RSI, MACD, ADX and so on), with its conventional reference levels
- ▲ buy and ▼ sell markers at the fill prices
- gray shading where trading was paused by the drawdown limit

Hover anywhere to see a crosshair and a tooltip listing every indicator's value at that candle. Use the toolbar to zoom, pan, go back, or save the chart as an image. All panels share one time axis, so zooming one zooms them all.

For how indicators are drawn and how to hide some, see [Charts and tooltips](indicator_reference.md#10-charts-and-tooltips).

### Model value

The Model value tab draws every model's value over time on one chart, with these reference lines:

- a horizontal line at the starting capital
- a gray **buy and hold** line for each product and starting capital

With several models, the legend shows each model's ending value and return, and the model chosen in **Showing model** is drawn slightly heavier. Hover to read every model's value and buy-and-hold value at one moment.

### Trades

The Trades tab lists every completed trade of the chosen model, with a summary above the table:

- wins and losses
- net P&L
- average, best and worst return
- a count of each exit reason

Click a column heading to sort by it. Double-click a trade to zoom the chart to it; the toolbar's back button returns to the previous view.

| Column | Meaning |
|---|---|
| Entry, Exit (UTC) and prices | Fill times and fill prices, including slippage. |
| Held | How long the position was open. |
| Position | The cash committed to the trade, including the entry fee. |
| Fees | The fees for both ends of the trade. |
| P&L, Return | The profit after fees and before tax, in currency and as a fraction of Position. |
| Outcome | `Win` if the P&L is positive, otherwise `Loss`. |
| Value after | The model's total value after the exit. |
| Exit reason | Why the trade closed (see [Exit reasons](#exit-reasons)). |

### Metrics

The Metrics tab holds the full text report for the chosen model:

- the period, start date, warm-up and first entry
- headline results
- the indicators and rules
- the **friction model** actually used: fee rates and their source, slippage, the liquidity buffer, tax, and the friction-screen verdict
- the tax paid each year, with any loss carried forward
- flash-crash events and fills
- drawdown pauses
- the last 10 trades
- the settings the simulator does not model yet

**Load Profile** and **Test Coinbase Authentication** also write their reports here.

---

## 8. How trades are simulated

The backtest (`backtest.py`) is a long-only, spot simulation. It holds at most one strategy position at a time and uses no leverage or shorting. It processes each candle in order and never looks ahead: a rule evaluated at a candle's close sees only that candle and earlier ones.

### Order of events in each candle

1. **New UTC day:** the `max_orders_per_day` count resets.
2. **New calendar year:** tax on the finished year is settled (see [Taxes](#taxes)).
3. **Flash crashes**, if `flash_crash_buy` is set: any resting limit buy the candle traded down to fills. What those buys hold is sold at the close of the candle in which the crash recovers.
4. **Exits**, if a position is open, checked in priority order:
   1. **Stops.** These are the hard stop, the trailing stop and the drawdown stop. If the candle's low reaches any of them, the highest one triggered fills.
   2. **Profit lock.** If the candle's high reaches the target, the position sells at the target.
   3. **Exit rules.** If the exit rules hold at the close, the position sells at the close.

   When a stop and the target are both reached within one candle, the stop is assumed to come first, which is the conservative choice. During a flash crash lasting over an hour, no exits happen until the crash recovers.
5. **Drawdown limit** (the mode without a window): if the model's value at the close has fallen `max_drawdown_limit` below its peak, the position is sold at the close and trading pauses.
6. **Entries**, if no position is open. The model buys at the close when all of these hold:
   - the entry rules hold
   - trading isn't paused
   - the daily order limit allows it
   - with a drawdown window, there is room above the limit for the trade's costs
   - the signal passes the friction screen

   A position closed by a stop or target earlier in the same candle can be reopened here if the entry rules still hold.

### Fills, fees and slippage

| Event | Fill price | Fee |
|---|---|---|
| Entry (`ENTRY_SIGNAL`) | close × (1 + slippage) | taker |
| Exit rule, drawdown limit | close × (1 - slippage) | taker |
| Stop | the stop price, or the open if the candle gapped below it, × (1 - slippage) | taker |
| Profit lock | the target price, or the open if the candle gapped above it, × (1 - slippage) | taker |
| Flash-crash buy | the level's limit price | maker |
| Flash-crash rebound sale | close × (1 - slippage) | taker |

- The **hard stop** sits at entry fill price × (1 - `hard_stop_percentage`).
- The **profit target** sits at entry fill price × (1 + `profit_lock_target`).
- The **trailing stop** sits at the highest high since entry × (1 - `trailing_stop`).
- **Position size:** an entry spends (cash - flash-crash reserve) × `target_position_fraction`. The fee comes out of that amount.

### Exit reasons

| Reason | Meaning |
|---|---|
| `EXIT_SIGNAL` | The exit rules held at the close. |
| `PROFIT_LOCK` | The price reached `profit_lock_target`. |
| `HARD_STOP` | The price fell to the hard stop. |
| `TRAILING_STOP` | The price fell `trailing_stop` below its high since entry. |
| `DRAWDOWN_STOP` | The model's value reached the windowed drawdown limit, so trading pauses. |
| `DRAWDOWN_LIMIT` | The close-based drawdown limit was hit, so trading pauses. |
| `FLASH_REBOUND_SELL` | A flash-crash buy was sold when the crash recovered. Its entry reason is `FLASH_CRASH_BUY_<drop>`, such as `FLASH_CRASH_BUY_30%`. |

### Taxes

Each calendar year's realized gains and losses are netted. When the first candle of the next year arrives, `tax_buffer_rate` is paid on a net gain, from cash, and a net loss is carried forward. Tax payments count as withdrawals, not trading losses, in all drawdown figures. Tax on the unfinished final year is reported as an estimate and is not deducted from the ending value.

### Drawdown limit

`safeguards.max_drawdown_limit` works in one of two modes, depending on whether `drawdown_window` is set. After either kind of drawdown exit, trading resumes only when the entry signal switches off and then fires again. For details, see [The two drawdown modes](model_configuration.md#the-two-drawdown-modes).

### Flash crashes

With `strategy.flash_crash_buy`, the simulator handles crashes in two ways:

- **Brief crashes** (recovered within an hour) are treated as bad data and hidden from the strategy.
- **Longer crashes** are held through.

Either way, the limit buys catch the dip. For details, see [flash_crash_buy](model_configuration.md#flash_crash_buy).

### Start date and warm-up

Indicators are computed over the whole downloaded history, so the history before your start date warms them up and they are fully formed on the first day of the test.

From the start date on:

- each model holds its starting capital in cash and waits for its own entry rules
- returns, buy and hold, drawdown, taxes and the charts are all measured from the start date

The Metrics tab reports:

- the actual start
- how many days of warm-up were available
- when the first entry came

If the date is before the first available candle, the test starts at the first candle, and the Metrics tab says so.

Without a start date, the test begins at the first candle. Each indicator then has no value until it has enough history, and no rule can fire during that warm-up, so the model sits in cash.

### What the results do not include

Backtests flatter strategies. Keep these limits in mind:

- **Fills are idealized.** Orders fill at candle prices, plus your slippage setting, whatever their size.
- **Today's fee tier is applied to the whole history.**
- **Taxes are one flat rate** on calendar-year net gains. Short-term and long-term rates are not distinguished.
- **An open position at the end is valued at the last close**, without selling costs. The final year's estimated tax is not deducted.
- **Idle cash earns nothing.**
- **Flash-crash profits are an upper bound.** Candles can't show whether a resting order would really have filled.
- **Gaps in the data shift periods.** Periods count candles, and candle data has gaps where nothing traded, so a period can span more time than its nominal length on a quiet product.
- **Picking the best run is optimistic.** If you try many variants on the same history and keep the best, the winner's results overstate what to expect in future.
- **Some profile settings are not modeled yet.** Each report lists them under "Not modeled yet".

---

## 9. Scripting a backtest without the window

The simulator's building blocks are plain Python functions, so you can run backtests from a script. For example, you can sweep a parameter or schedule a nightly run.

This script runs a profile against the cached candles with no network access. Save it in the repository root, for example as `my_backtest.py`, and run it from there:

```python
import json
from datetime import datetime, timezone

from backtest import DEFAULT_CACHE_DIR, load_cached_candles, resolve_granularity, run_backtest
from friction import FrictionModel
from simulator import describe_profile, format_backtest_result

with open("config/btc_macro_horizon.json", encoding="utf-8") as handle:
    profile = json.load(handle)

valid, report = describe_profile(profile)  # the same check as Load Profile
if not valid:
    raise SystemExit(report)

granularity, _ = resolve_granularity(profile["period_granularity"])
candles, _, _ = load_cached_candles(DEFAULT_CACHE_DIR, profile["product_id"], granularity)

start = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp())
result = run_backtest(candles, profile, friction=FrictionModel(profile), start_time=start)

print(format_backtest_result(result))
print(f"{result['total_return']:+.2%} over {result['trade_count']} trades")
```

The script has two dependencies:

- **The cache must already exist.** Run the simulator once for that product and candle size first. `load_cached_candles` returns `None` when there is no cache.
- **Without live inputs**, `FrictionModel(profile)` uses the profile's fallback fee rate and no Bitcoin network fee. The report shows this as `profile fallback (live rates not fetched)`.

To use live fees and to download data from a script, create a client the same way the simulator does. Continuing the script above (these calls need your `.env` key):

```python
from backtest import load_candles, lookback_window
from engine import QuantitativeTradingEngine
from friction import fetch_friction_inputs

client = QuantitativeTradingEngine(base_directory=".", node_is_primary=False).create_coinbase_client()
granularity, seconds = resolve_granularity(profile["period_granularity"])
candles = load_candles(client, profile["product_id"], granularity, *lookback_window(seconds))
friction = FrictionModel(profile, fetch_friction_inputs(client, profile))
```

Useful functions:

| Function | Module | Purpose |
|---|---|---|
| `describe_profile(profile)` | `simulator` | Validates a profile. Returns `(is_valid, report_text)`. |
| `validate_profile(profile, granularity_seconds)` | `model_config` | Returns the list of indicator and rule problems; empty if there are none. |
| `run_backtest(candles, profile, starting_capital=None, progress=None, friction=None, start_time=None, wait_for_fresh_signal=False)` | `backtest` | Runs one backtest and returns a result dictionary. |
| `format_backtest_result(result)` | `simulator` | The Metrics-tab text for a result. |
| `load_candles(client, product_id, granularity, start, end, cache_dir=..., progress=None)` | `backtest` | Cached download of candles in the range `[start, end)`. |
| `load_cached_candles(cache_dir, product_id, granularity)` | `backtest` | Reads the cache only. Returns `(candles, fetched_start, fetched_end)` or `None`. |
| `lookback_window(granularity_seconds)` | `backtest` | The 10-year `(start, end)` the simulator uses. |
| `parse_start_date("2022-01-01")` | `simulator` | Converts a date string to the `start_time` timestamp. |

The main keys of the result dictionary are:

- `total_return`, `ending_equity`, `starting_capital` and `buy_and_hold_return`
- `max_drawdown` and `max_window_drawdown`
- `trade_count`, `win_rate`, `fees_paid`, `taxes_paid` and `screened_signals`
- `trades`, a list of dictionaries with entry and exit times, prices, P&L, fees and reasons
- `friction`, which describes the rates used
- `series`, the time, close, equity and indicator arrays behind the charts

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Select at least one model in the Models list` | Nothing is selected. Click a model; if the list is empty, add a profile to `config/` and click **Refresh**. |
| `Fix these profiles first ...` | A selected profile is invalid. Click **Load Profile** to see every problem, then see [Validation and troubleshooting](indicator_reference.md#8-validation-and-troubleshooting). |
| `Coinbase API key name and private key are required ...` | `.env` wasn't found or is incomplete. Run the simulator from the repository root, and check `.env` (see [trader.md](trader.md#2-setting-up-the-env-file)). |
| `Coinbase API test failed: HTTP Error 401 ...` | The key name or private key is wrong, or the key has expired. If the key has an IP allowlist, add the outbound IP address shown in the message. |
| `... HTTP Error 403 ...` | The key works but lacks permission. Enable **View** on the key. |
| `Could not reach the Coinbase API` | A network problem, or a wrong `COINBASE_BASE_URL`. |
| A dialog asks to install the Coinbase SDK | `coinbase-advanced-py` isn't installed in the Python running the simulator. Accept, or run `pip install -r requirements.txt` in your virtual environment. |
| A dialog asks to add Python to `PATH` | The running Python's folder isn't on `PATH`. Declining is harmless when the right interpreter is already running. Accepting adds the folder to your user `PATH`, persistently on Windows. |
| `Only LONG_ONLY strategies can be simulated so far` | Set `strategy.direction` to `LONG_ONLY` or remove it. |
| `The start date ... is after the last candle` | Choose an earlier date. |
| The run finishes with no trades | Check **Entry signals blocked by the screen** in Metrics first (see the [friction screen](model_configuration.md#5-the-friction-screen-worked-through)). For more checks, see the [no-trades checklist](indicator_reference.md#8-validation-and-troubleshooting). |
| Fees show `profile fallback (...)` | The live fee lookup failed (the reason is shown), so the profile's fallback rate was used. |
| The first run is slow | It is downloading the full history; later runs use the cache. Larger candle sizes download much faster (see [section 6](#6-historical-data-and-the-cache)). |
| The chart is crowded | Hide less important indicators with `"plot": false`, or enlarge the window. |

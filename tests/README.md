# tests/: the test suite

These are automated tests for every module. They run offline: Coinbase is replaced by fake clients, files go to temporary folders, and charts render without a window. You need no API key and no network connection.

## Running the tests

From the **repository root**, with the virtual environment active:

```bash
pip install pytest            # once; pytest is not in requirements.txt
python -m pytest              # the whole suite
python -m pytest -q           # quieter output
python -m pytest tests/test_risk.py          # one file
python -m pytest -k flash_crash              # tests whose names match
```

Use `python -m pytest`, not plain `pytest`. The tests import the project modules (`backtest`, `engine` and so on) and shared helpers from `tests.test_backtest` and `tests.test_indicators`. The repository has no `conftest.py` or pytest configuration that sets the import path. `python -m` puts the current folder, the repository root, on the import path; plain `pytest` does not, and every file fails at collection with import errors.

## What each file covers

| File | Covers |
|---|---|
| `test_backtest.py` | Candle granularities and the 10-year window, paging through the candle API without gaps or duplicates, the incremental cache, and the core backtest: entries, exits, hard stops, drawdown pauses, commission on both fills, the model value recorded for every candle, starting capital (and rejecting invalid values), and the report and trade-table text. It also defines the shared helpers. |
| `test_performance_log.py` | The model performance log: adding new models, leaving the file untouched after an identical run, replacing only the changed model's row while keeping its first-recorded time, updating a row when the profile changes, upgrading an older file with fewer columns, and the status-line summary. |
| `test_candle_store.py` | The candle database: importing an old CSV cache; that each windowed indicator's lookback matches what its values depend on; metric keys; storing slow indicators as columns and filling in only new candles; not storing fast or history-dependent ones; later windows; recomputing a column whose stored values no longer match; models sharing indicators, with and without flash-crash buys; and backtests giving the same results with and without the store. |
| `test_rules.py` | The rule language: comparisons and logic, unknown values during warm-up, when a rule becomes ready to evaluate (every indicator and lookback warmed up), cross and history functions, duration periods, division by zero giving an unknown value, and rejecting unsafe formulas. Also compiled models producing entry and exit signals, reporting every problem at once, entry rules being required and exit rules optional, checking indicator sources before a run, legacy-profile translation, and a check that **every template in `config/.example/` is valid**. |
| `test_indicators.py` | Period parsing, and indicator values against reference calculations: SMA, WMA, the EMA family, RSI, ATR, Bollinger, MACD, Stochastic, Williams %R, Donchian, the volume indicators and daily VWAP. Also that the trend and volatility indicators stay in range and warm up, indicators fed from other indicators, reporting source loops and bad references, and every type running on realistic data. |
| `test_friction.py` | The friction model: live and fallback fee rates, the `config` and `disabled` sources, the fee buffer multiplier, the friction screen (liquidity buffer, network fee, tax), lookup failures, yearly tax with losses carried forward, and blocked signals. |
| `test_risk.py` | Risk handling: the windowed drawdown stop and old highs leaving its window, entry room above the floor, tax treated as a withdrawal, the trailing stop, and flash crashes (limit fills, level validation, a reserve the strategy never trades, crashes held through at their real prices, the drawdown limit waiting for recovery, crashes that never recover, and stops that gap through). |
| `test_start_date.py` | Starting a test later with warmed-up indicators, waiting for a fresh signal, start dates outside the data, parsing start dates, where buy and hold starts (the first candle the model could buy on), and the start-date and buy-and-hold report lines. |
| `test_model_search.py` | The model search (`model_search.py`): library conditions validating on daily candles, recovering reference vote profiles as genomes, built profiles passing validation, persistence snapped to whole candles, mutation and crossover keeping vote thresholds in range, and characteristic lifts favouring the best models. |
| `test_simulator_run.py` | The simulator's background run: the API is tested before any download, a failed API test stops the run, models share downloads, each model keeps its own capital, model naming and grouping, the comparison table (first buy, B&H start, vs buy and hold), and recording each run in the model performance log. |
| `test_simulator.py` | Loading a profile file, and parsing the chart floor you type in (blank, numbers with `$` and commas, and rejecting text, zero, negatives and infinity). |
| `test_chart.py` | The charts, rendered with matplotlib's Agg backend: price, indicator and trade drawing, hover tooltips, zooming to a trade, the model-value chart (its start and end labels, several models at once, and where its buy-and-hold line starts), the chart floor (including a floor above every value, which is reported and ignored), oscillator panels sharing one time axis, line styles, and `"plot": false`. |
| `test_engine.py` | The trader engine: ledger creation, profile loading, `.env` parsing, key files, missing credentials, authentication through a fake REST client (including the outbound IP shown on HTTP 401), the configured API host, authenticating a client you supply without creating a new one, and `tax_allocation` schedules (including yearly ones) passing through. |

## Shared helpers

Two files define helpers that other files import. From `test_backtest.py`:

| Helper | Purpose |
|---|---|
| `_candle(start, close, low=None, high=None, open_=None)` | Builds one candle dictionary. Prices you leave out default to `close`. |
| `_profile(**overrides)` | Builds a small, valid EMA-crossover profile. Keyword arguments replace top-level fields. |
| `FakeCandleClient` | Stands in for the Coinbase client. It serves a flat-priced candle for every minute and records each request it receives. |

From `test_indicators.py`, used by `test_candle_store.py`, `test_rules.py` and `test_chart.py`:

| Helper | Purpose |
|---|---|
| `_candles(count=400, seed=7)` | Builds a deterministic random walk of 15-minute candles with realistic highs, lows and volume. |
| `FIFTEEN_MINUTES` | The candle size those candles use, in seconds (900). |
| `_assert_close(actual, expected)` | Compares two series to within 1e-9, treating matching `NaN` warm-up values as equal. |

## Adding tests

Name new files `test_*.py` and their functions `test_*`. Reuse the helpers above. Keep tests offline by passing fake clients and using pytest's `tmp_path` for files. Never use the real `.env` file or the network.

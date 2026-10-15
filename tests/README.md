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

Use `python -m pytest`, not plain `pytest`. The tests import the project modules (`backtest`, `engine` and so on) and shared helpers from `tests.test_backtest`. `python -m` puts the repository root on the import path; plain `pytest` does not, and fails at collection with import errors.

## What each file covers

| File | Covers |
|---|---|
| `test_backtest.py` | Candle granularities and the 10-year window, paging through the candle API without gaps or duplicates, the incremental cache, and the core backtest: entries, exits, hard stops, drawdown pauses, commission on both fills, starting capital, and the report and trade-table text. It also defines the shared helpers. |
| `test_performance_log.py` | The model performance log: adding new models, leaving the file untouched after an identical run, replacing only the changed model's row while keeping its first-recorded time, updating a row when the profile changes, upgrading an older file with fewer columns, and the status-line summary. |
| `test_candle_store.py` | The candle database: importing an old CSV cache; that each windowed indicator's lookback matches what its values depend on; metric keys; storing slow indicators as columns and filling in only new candles; not storing fast or history-dependent ones; later windows; recomputing a column whose stored values no longer match; models sharing indicators, with and without flash-crash buys; and backtests giving the same results with and without the store. |
| `test_rules.py` | The rule language: comparisons and logic, unknown values during warm-up, when a rule becomes ready to evaluate (every indicator and lookback warmed up), cross and history functions, duration periods, and rejecting unsafe formulas. Also profile compilation, reporting every problem at once, legacy-profile translation, and a check that **every template in `config/.example/` is valid**. |
| `test_indicators.py` | Period parsing, and indicator values against reference calculations: SMA, WMA, the EMA family, RSI, ATR, Bollinger, MACD, Stochastic, Williams %R, Donchian, the volume indicators and daily VWAP. Also warm-up, indicators fed from other indicators, source loops, and every type running on realistic data. |
| `test_friction.py` | The friction model: live and fallback fee rates, the `config` and `disabled` sources, the fee buffer multiplier, the friction screen (liquidity buffer, network fee, tax), lookup failures, yearly tax with losses carried forward, and blocked signals. |
| `test_risk.py` | Risk handling: the windowed drawdown stop, entry room above the floor, tax treated as a withdrawal, the trailing stop, and flash crashes (limit fills, reserves, crashes held through at their real prices, the drawdown limit waiting for recovery, crashes that never recover, and stops that gap through). |
| `test_start_date.py` | Starting a test later with warmed-up indicators, waiting for a fresh signal, start dates outside the data, parsing start dates, where buy and hold starts (the first candle the model could buy on), and the start-date and buy-and-hold report lines. |
| `test_simulator_run.py` | The simulator's background run: the API is tested before any download, a failed API test stops the run, models share downloads, each model keeps its own capital, model naming and grouping, the comparison table (first buy, B&H start, vs buy and hold), and recording each run in the model performance log. |
| `test_simulator.py` | Loading a profile file. |
| `test_chart.py` | The charts, rendered with matplotlib's Agg backend: price, indicator and trade drawing, hover tooltips, zooming to a trade, the model-value chart and where its buy-and-hold line starts, the chart floor, oscillator panels sharing one time axis, line styles, and `"plot": false`. |
| `test_engine.py` | The trader engine: ledger creation, profile loading, `.env` parsing, key files, missing credentials, authentication through a fake REST client (including the outbound IP shown on HTTP 401), the configured API host, and `tax_allocation` passing through. |

## Shared helpers

`test_backtest.py` defines helpers that other files import:

| Helper | Purpose |
|---|---|
| `_candle(start, close, low=None, high=None, open_=None)` | Builds one candle dictionary. Prices you leave out default to `close`. |
| `_profile(**overrides)` | Builds a small, valid EMA-crossover profile. Keyword arguments replace top-level fields. |
| `FakeCandleClient` | Stands in for the Coinbase client. It serves a flat-priced candle for every minute and records each request it receives. |

## Adding tests

Name new files `test_*.py` and their functions `test_*`. Reuse the helpers above. Keep tests offline by passing fake clients and using pytest's `tmp_path` for files. Never use the real `.env` file or the network.

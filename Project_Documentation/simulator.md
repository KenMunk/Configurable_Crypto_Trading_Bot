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

The window opens with the title **Quant Bot Simulator**. The simulator reuses the trader engine's authentication code. As a side effect, it creates an empty `ledger/fortress.db` in the current directory when it starts.

---

## 3. Your first run

1. **Select a model** in the **Models** list.
2. Click **Load Profile**. The **Metrics** tab confirms the profile is valid and lists its starting value, flash-crash buys, indicators and rules, or it lists every problem it found.
3. *(Optional)* Click **Test Coinbase Authentication** to check your API key on its own.
4. *(Optional)* Type a **Start testing from** date, such as `2022-01-01`.
5. Click **Run Simulation** and watch the three progress bars. The first run for a product and candle size downloads the full history, which can take several minutes. Later runs load it from the cache.
6. Read the results. With one model, the **Chart** tab opens. With several, the **Comparison** tab opens.

---

## 4. The simulator window

From top to bottom:

### Models

Models are kept in one folder per currency, such as `config/DOGE-USD/` (see [config/README.md](../config/README.md)). The models are listed in two lists, each named after its file with its currency beside it: `doge_b_130   (DOGE-USD)`.

- **Available**, on the left, lists the models you haven't chosen. **Currency** (top right) narrows it to one currency; **Type** beside it to models with [market regimes](model_configuration.md#49-regimes-experimental) (**Regime models**) or without them (**Models without regimes**), which the list tags `regimes`; and **Search** to models whose names contain every word you type, such as `doge b`. Its count says how many it shows, such as `Available: 239 of 447`.
- **Selected**, on the right, lists the models the next run tests, in the order you added them, whatever the currency filter shows.
- **Add ›** moves the models highlighted in Available to the end of Selected; so does double-clicking one, or pressing **Enter**. Ctrl-click or Shift-click to highlight several. **Add all ››** moves every model Available shows, so filter or search first to add a whole group.
- **‹ Remove** moves the models highlighted in Selected back to Available; so does double-clicking one, or pressing **Delete**. **‹‹ Remove all** empties Selected.
- **Move up** and **Move down** change the order of the highlighted Selected models.
- Models a ranking moved to a currency's `discard/` folder aren't listed. Tick **Include discarded models** to list them too, after the others and marked `discarded`; you can then add and test them like any other.
- A **★** marks each currency's best model: the one with the highest full-history return the simulator has tested for that currency (see below).
- When the window opens, each currency's ★ model is in Selected. Before any has been recorded, `btc_macro_horizon` is if it exists; otherwise the first model is.
- **Add file...** adds profiles from anywhere on disk and puts them in Selected.
- **Refresh** picks up files added to or removed from `config/`, and keeps Selected as it is. Files added with **Add file...** stay in the list while they still exist.
- If two files share a name, the second appears as `name (2)`.

**Each currency's historically best models.** A **▲** marks each currency's top models by full-history growth in the performance log (5 by default; set in **Test options...**). A row logged for an earlier version of a profile doesn't count. **Select top models** puts them in Selected (replacing what was there), or only those passing the Currency and Type filters when they are set, so testing can focus on the strongest candidates.

**Each currency's best model.** When a run finishes, the simulator finds the best model of each currency it tested, by return, and:

- **records it** in that currency's folder as `best_model.json`, when the run tested the full history (no start date) and the model beats the return already recorded. A run of a single weaker model never replaces a better record, and a record whose model has been deleted or discarded no longer counts.
- **selects it** in the Models list, in place of your selection, so the next run starts from the best of each currency. Untick **After a run, select the best model of each currency tested** to keep your own selection.
- **shows it**: with several currencies, the charts show each currency's best; with one, its best few, as before. The status line lists each currency's best, such as `best per currency: BTC-USD btc_15m_02 +7148%, DOGE-USD doge_b_130 +2270%`.

### Start testing from

- **The date box** (UTC, optional) starts the test at a moment you choose. It accepts `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` and rejects future dates. Leave it blank to test the full 10 years. See [Start date and warm-up](#start-date-and-warm-up).
- **Wait for a fresh entry signal**: when ticked, a model ignores an entry signal that is already true on the start date. It waits for the signal to switch off and fire again, so a start in the middle of a rally doesn't buy the top.

### Buttons

| Button | What it does |
|---|---|
| **Load Profile** | Validates every selected profile and writes a report to the **Metrics** tab, one section per profile headed `=== name ===`. With one profile selected, the report also shows the profile's JSON. A profile in the original fixed-EMA format gets a note that it was translated into indicators and rules. |
| **Run Simulation** | Backtests every selected profile. The button is disabled while a run is in progress. |
| **Test Coinbase Authentication** | Makes a read-only account request with your key and shows the full result in the **Metrics** tab. |
| **Clear data...** | Opens a dialog to clear stored data; see [Clearing stored data](#clearing-stored-data). |

Next to the buttons, the **Mode** button switches between running models at once and one after another. Click it to toggle:

- **Mode: parallel (multi-process)**, the default, runs several models at once on worker processes. **Workers** sets how many.
- **Mode: single-threaded** runs the models one after another, in the window's own background thread. **Workers** is grayed out.

To see what parallel mode saves, run the same models once in each mode and compare the **Time spent** lines under the progress bars (see [Progress](#progress)).

In parallel mode, **Workers** sets how many worker processes run at once. The rule is **one model, one set of data, one thread**:

- Each worker runs one model on one set of data at a time (its real history, or one path of one scenario), on one thread: worker processes run numpy's maths libraries single-threaded. A history run is a task per model; a scenario or stress run is a task per model per scenario path.
- A history run never uses more workers than it has models, since each model is one task; 3 models use at most 3 workers, whatever the Workers box allows.
- Scenario and stress runs share every model's paths across all the workers. A free worker takes the next path of the model with the fewest running, so while several models have paths left they are spread one per worker, and when only a few models are left, idle workers take more of their paths instead of waiting. A single model's stress test uses every worker. One model through the 133-combination stress plan on 6-hour BTC candles took 38 s on one worker and 19 s on four.
- The default, `auto`, allows every CPU but two. Scenario and stress runs add one worker per GPU (see [Stress tests](#stress-tests)); history runs don't, as their indicators come from the candle store, so a GPU only adds load.

Small runs can be slower in parallel than single-threaded: each worker takes about 3 s to start and to load its market's candles and stored indicators, which one process does once for every model. 20 daily DOGE models took 0.3 s single-threaded and 6.5 s on 12 workers; 20 BTC 15-minute models, 29 s and 18 to 24 s. Switch **Mode** to single-threaded for quick runs of daily models.

A number typed in **Workers** is used as it is (for history runs, still at most one worker per model). Of the CPUs, one spare CPU keeps the window and the candle writer (the price history downloads) responsive, and the other runs the gatherer, which consolidates the results the workers return. On a 12-CPU computer that leaves at most 10 workers; on one or two CPUs it is still one worker. The hint beside the box shows that number for your computer. A number above it is lowered to it. At `1`, models run one after another, as in single-threaded mode. A history run of a single model always runs on its own. See [Running models in parallel](#running-models-in-parallel).

**Memory limit**, beside the Workers box, keeps a run from filling the computer's memory. Every run keeps each result's chart data (price, model value and indicator series), and a stress test of many models makes hundreds of thousands of results: enough to exhaust the memory and stop the computer responding. The limit is a share of the memory that was free when the simulator launched, 80% by default; the hint beside the box gives that amount. A run is measured by how far the free memory has fallen since launch, so its worker processes count too. As it fills its limit, the run slows down rather than stopping:

| Share of the limit in use | Chart data | Models or scenario paths running at once |
|---|---|---|
| under 60% | kept in memory | every worker |
| 60% and over | written to disk, under `data/chart_spill/` | every worker |
| 80% and over | on disk | half the workers |
| 90% and over | on disk | a quarter of them |
| at the limit | on disk | one |

When chart data starts going to disk, the results already in go too. A chart or table reads a result's chart data back from its file when it first needs it, and the 16 most recently read stay in memory, so the charts keep every point and nothing is cancelled: every model and path runs, and the status line says how many results' chart data went to disk and how far the run slowed. A run's files are deleted once the next run's results replace its own, and those left by earlier sessions when the simulator launches (another simulator window's are left alone). They take about 0.3 MB per stress result: 20 daily BTC models through the stress plan with 3 paths made 6,620 results and 1.8 GB of files.

Memory taken by other programs during a run counts against the limit too. Where the free memory can't be read, the hint says so and there is no limit. Those 20 models took 76 s at the default 80% (8.1 GB at most of a 13.4 GB limit, chart data on disk, never slowed); under a 15% limit (2.5 GB) they ran one task at a time for 189 s and peaked at 2.7 GB, a little over, since the idle worker processes alone take much of so small a limit. See `memory_guard.py`.

### Progress

| Bar | Tracks |
|---|---|
| **API check** | The authentication test, then the fee lookups. |
| **Data gathering (10 years)** | Candle downloads or cache loads, one product and candle size at a time. |
| **Simulation** | Each model in turn: its indicators, then every candle of its test. With parallel workers, it moves on as each model finishes, such as `3 of 12 models done (btc_15m_04)`. |

Under each bar, a line shows its percentage and what it is doing, such as `BTC-USD FIFTEEN_MINUTE: 350,640 candles loaded from cache`. Before a run, it reads `waiting`.

**Time spent**, below the bars, counts up while a run is under way, such as `12.3 s so far (8 models, parallel, 8 workers)`. When the results appear, it shows the run's total time, its model count and its mode. The time runs from clicking **Run Simulation** to the results, so it includes the API check and data gathering, which take the same time in either mode. A run that stops with an error shows its time but isn't used for the comparison.

The line under it compares the last successful run in each mode, once both modes have run, such as `Last single-threaded run: 10.5 s for 8 models; last parallel run (8 workers): 5.4 s for 8 models. Parallel was 1.9x as fast, saving 5.1 s`. The speed-up is given only when both runs tested the same models; otherwise the line says to run both modes on the same selection. Times are kept until the window closes. For a fair comparison, run each mode once first so that both use cached candles.

### Showing model

This drop-down picks which model the **Chart**, **Trades** and **Metrics** tabs show. After a run, it starts on the best performer. It lists the models in the charts' view. After a run, the view holds every model, or with more than 8 the 8 best by return, one for each distinct line color, so the Model value chart starts out readable.

To change which models are in view, use any of these:

- **The list beside the Model value chart.** See [Model value](#model-value).

- **Choose models...** opens a list of every model with its return. Type in **Search** to narrow the list: a model is listed when its name contains every word you type, such as `btc 15m`. Click, Ctrl-click or Shift-click to choose models. **Select listed** and **Clear listed** choose or drop every listed model. Models you chose before a search stay chosen while the search hides them. Click **Apply** or press **Enter** to show them; **Cancel** or **Esc** leaves the view as it was.
- **Select rows in the [Comparison](#comparison) tab** and click **Chart selected models**, or press **Enter**.

Either way, **Model value** draws only the models in view, and **Showing model** lists only them. Each model keeps its line color however the view changes. A line beside the buttons says how many models are in view, such as `Charts show 5 of 40 models`. **Show all** brings every model back. The model on show stays if it is still in view; otherwise the best model in view by return takes its place. All three ways change the same view, so the Chart tab, the Model value tab and the gray rows in the Comparison tab always agree.

### Full screen

The **Full screen (F11)** button at the right end of the **Showing model** row fills the screen with the results. It hides the model list, the start date, the buttons and the progress bars, so the charts get the room. Press **Esc**, press **F11** again, or click **Exit full screen (Esc)** to return; the hidden controls come back as they were. The status lines stay visible, so a run started before going full screen can still be followed.

### Status

The bottom of the window shows two status lines: the simulation status (what is running, or the last error), and the result of the last Coinbase authentication.

---

## 5. What happens during a run

When you click **Run Simulation**, the simulator works through these steps:

1. **Validation.** The start date is checked, then every selected profile. If any has a problem, nothing runs, and a dialog lists the first problem in each failing profile. **Load Profile** lists every problem.
2. **Client set-up.** The simulator creates a Coinbase client from your `.env` settings. If the Coinbase SDK (`coinbase-advanced-py`) is missing, it asks whether to install it into the running Python. If Python's folder isn't on `PATH`, it offers to add it (see [Troubleshooting](#10-troubleshooting)).
3. **API check.** A read-only request lists your accounts. If it fails, the run stops before downloading anything, and the status line explains why.
4. **Fee lookups**, once for the whole run:
   - **Your live Coinbase fee tier**, if any selected profile uses `commission_rate_source: "exchange_api"`, which is the default.
   - **The Bitcoin network fee** from [mempool.space](https://mempool.space), if any selected profile trades a `BTC-` product.

   If the fee tier lookup fails, the run continues with the profile's fallback rate. If the network fee lookup fails, the run continues without a network fee. Either way, the Metrics tab gives the reason. When both lookups are done, the **API check** bar shows the fee rates of the first selected model.
5. **Data gathering.** Candles are loaded for each distinct product and candle size. Models that share both also share one download.
6. **Simulation.** Each model is backtested in turn.
7. **Performance log.** Each model's results are recorded in the [model performance log](#model-performance-log).
8. **Results.** The tabs fill in, and the **Showing model** picker selects the best model by return. The status line reports the return and trade count (or, with several models, the best one) and what the performance log recorded.

There is no cancel button. Closing the window stops a run. A download interrupted that way is not saved, and the next run fetches that range again.

### Synthetic scenarios

History is one path the market happened to take. Synthetic scenarios test how models cope with others: a crash, a long bear market, a pump and collapse, a calm rally, sideways chop, a volatility shock, or many random paths stitched from real history.

**Test options...**, above the buttons, opens the options window; the line beside it shows the current choice.

- **Data to test on**: **Historic data only** (as before), **Historic data plus synthetic scenarios**, or **Synthetic scenarios only**.
- **Focus**: how many of each currency's historically best models to mark ▲ (see [Models](#models)).
- **Stress test**: see [Stress tests](#stress-tests) below.
- **Synthetic scenarios**: the scenario set to use, from `scenarios/`, listed as **modules**. Select one to edit it on the right (fields the type doesn't use are greyed out) and click **Apply changes to the scenario**.
  - **Chains.** A module with **Continues the scenario above (chained)** ticked joins the run of the module above it: the run's modules play one after another as one continuous path, each starting where the one before ended, such as a crash, then three months of chop, then a calm rally. The **Run** column shows each module's run and step (`Run 3 · step 2 of 3`), and chained modules are shown in blue. **Chain to above** and **Unchain** do the same from the buttons. A run's placement (after history, or at a date) is its first module's.
  - **Drag a row** to move it; **Move up/down** do the same. **Duplicate** copies the selected module right after it (a chained copy joins the same run), and **Add new** and **Remove** edit the list.
  - **Save set** and **Save set as...** write the set. **Preview the selected module's run** draws the whole run on the chosen market's history (for runs with several paths, up to 10 of them), with a red line where it starts.

  Every scenario type and setting is described in [scenarios/README.md](../scenarios/README.md). Synthetic paths look like real markets: crashes fall fast and slow down, pumps accelerate into the top, recoveries ease in, and every candle moves as much as the market's own do, with fat tails and calm and violent stretches, while each phase still ends where it says.
- **Save** keeps the options (in `data/simulator_options.json`) for the next session.

With synthetic data, each selected model also runs on every path of every scenario, built from its market's history at its candle size. These runs:

- are named `<model> | <scenario>` (with ` #n` for each path of a multi-path scenario) and appear in the Comparison table, whose **Data** column names the scenario (`Historic` for real history). Filter it to compare one scenario;
- are measured from the scenario's first candle, with indicators warmed up on real history, so Return is the model's result through the scenario and Buy & hold is the market's;
- are never recorded in the performance log or as a currency's best model: only historic results are.

The **Scenarios** tab shows one row per model and one column per data source: the historic return, then each scenario's return (for several paths, the median with the worst path), the model's **Worst scenario**, and in how many scenario runs it **Beat buy & hold**. Rows are sorted by the worst scenario, so the most robust models come first. Synthetic runs come after the historic run. In parallel mode each model on each scenario path is its own task on the workers (and GPUs), shown in the **Workers** table; in single-threaded mode they go one after another. Resampled scenarios with many paths multiply the work.

The **Scenario charts** tab gives every data set the run tested on a chart of its own. A data set is the real history, or one path of one scenario (or of one stress combination, alone or with history), for one market. On the left, the data sets are listed by market, such as `ETH-USD ONE_DAY`, then by scenario. A scenario with several paths is one row, with its paths under it, `path #1` to `path #20`. Each row shows how many models ran on it, the best model's return (for several paths: the median, with the worst path) and buying and holding through it.

- Click a row to chart it. Clicking a scenario with paths charts its first path, and **Previous** and **Next** step through every data set and every path in order.
- The chart shows each model's value on that data set against buying and holding through it, titled with the data set's name. A dashed line marks where a scenario starts. Hovering reads every model's value at that moment, as on the Model value tab.
- Each model keeps one color on every data set's chart. There is no tax reserve panel here, so the value chart gets the whole height; the **Model value** tab still shows it.

A scenario only tests what it describes. The calm-trend DOGE models, for example, stay in cash through the default crash, bear, pump and volatility scenarios (0% while the market falls up to 70%), and only trade in a calm rally: that shows both their protection and what they need to make money.

### Stress tests

A stress test pushes the selected models through a fixed set of scenario combinations, to find their limits. Choose **Stress test** in **Test options...**, select the models to test (for example with **Select top models**), and click **Run Simulation**. Each model is run on its real history alone, as its **baseline**, then on every combination of the **stress test plan**, twice: **alone**, measured from the scenario's first candle, and **with history**, over the whole real history with the scenario appended, which shows whether a scenario wipes out the model's historic gains. Resampled scenarios contribute up to the number of paths set in the Stress test line.

**Speed.** In parallel mode (the **Mode** button), the stress test shares its runs out to worker processes: the **Workers** count (auto uses every CPU but two), plus one more per NVIDIA GPU when CuPy is installed (see `requirements-gpu.txt`). Each model's run on its real history, and each model on each path of each combination, is its own task (the latter alone and with history, on candles the task builds itself). Each worker runs one task at a time; the least busy model's next task goes first, and near the end, when fewer models than workers have tasks left, a model's tasks run on several workers at once so none sit idle (see the rule under [Buttons](#buttons)). Unlike history runs, stress tests gain a lot from the GPUs: every scenario path recomputes its indicators, and the GPUs do the moving averages, with the same results to the last digit. Three models (BTC 15-minute, SOL hourly, ETH daily) on 6 combinations, 36 runs, took 121 s single-threaded and 36 s on 3 workers with the two GPUs. The progress line counts the runs and names the workers and GPUs. While it runs, a **Workers** table under **Time spent** shows a row per worker: what it is running, such as `eth_growth_02: Nightmare: death spiral, -95% over 2 years #3 (with history) [GPU 1]` or `eth_growth_02: building ...` while it makes a scenario's candles, and a bar for how far through that run it is (`idle` between tasks). It updates a few times a second and disappears when the test ends; a single-threaded stress test shows one worker. Only the moving-average indicators (SMA, EMA, WMA, DEMA, TEMA, HMA) run on a GPU; the trading simulation itself always runs on the CPUs, so the GPUs show short bursts rather than steady use. In single-threaded mode, everything runs one after another, as before; the report is the same either way. Each worker holds a copy of the tested markets' history, so many workers on 15-minute markets need several GB of memory; the [Memory limit](#buttons) slows a stress test of many models down before it fills it.

**The stress test plan** is the list of combinations, kept in `scenarios/stress_plan.json` and edited on the **Stress test plan** tab of Test options. The first time, it is the built-in baseline: every built-in scenario run and nightmare on its own, plus 20 fixed pairs (one straight after the other), 33 combinations in all. Every stress test runs the same plan until you change it, so results are comparable from run to run. On the tab:

- **Add combination...** picks runs, in the order they should play, from the current scenario set and the nightmares; each continues from the one before.
- **Duplicate**, **Remove**, **Rename...** and dragging rows edit the list; the Nightmare column marks combinations with a nightmare in them.
- **Save** (at the bottom of the window) keeps your plan for every future run. **Reset to default** brings back the built-in baseline.

Each combination keeps its own copy of its scenario modules, so editing or deleting a scenario set later doesn't change the plan: the baseline stays the baseline.

The nightmares live in `scenarios/nightmares.json`, a scenario set you can open and edit like any other. It starts with five:

| Nightmare | What happens |
|---|---|
| Exchange collapse | -80% in two days, almost no bounce, then a further -40% bleed over six months |
| Euphoria then rug pull | +300% in a month, then -90% in a week |
| Whipsaw hell | Swings of about 35% either way at three times the market's volatility for six months |
| Death spiral | -95% over two years, with relief rallies |
| Crash cascade | Three -35% crashes, each after a partial recovery |

When the test finishes, the simulator writes a report folder, `data/stress_reports/<time>/`, and opens its summary in your browser:

- **index.html** ranks the tested models by their worst result, most robust first, with each one's historic baseline, runs, how many lost money, how many beat buy and hold, its median and worst return, worst drawdown, nightmares lost, and a verdict.
- **<model>.html**, one per model: the verdict (for example *Robust: it never lost more than 18%; beat buy and hold in 46 of 54 runs; in nightmares it lost money in 1 of 24*), headline numbers, the equity curves of its four worst runs against the market, and for each measurement (alone, with history) a chart of the model against buy and hold in every combination and a table with return, buy and hold, the difference, drawdown, trades, win rate, fees, taxes, and whether it lost money or beat buy and hold. Nightmare rows are marked ⚠.
- **report_<time>.pdf**, such as `report_20261022_153045.pdf` (the same time as the folder's name): the whole report in one file to print or share: the summary ranking, then each model's pages (its entry and exit rules, verdict, headline numbers and four worst runs, then for each measurement its chart of the model against buy and hold and its table of every run, worst first, 40 runs to a chart page and 28 to a table page). Pages are landscape US Letter; returns of 1,000% or more show in thousands in the tables, such as `+4.6k%`. The summary links to it and the status line gives its path. It takes about 6 s per model to write, so a stress test of 20 models spends about two minutes on it at the end.
- **results.csv**: every run's numbers, for a spreadsheet.

Verdicts: **Fragile** when the worst run lost half or more; **Weak** when more than half of the runs lost money; **Robust** when no run lost more than 20%; **Mixed** otherwise. The results also fill the Comparison and Scenarios tabs. Like other synthetic results, nothing from a stress test is logged or recorded as a currency's best model, its baseline included.

### Running models in parallel

In parallel mode with **Workers** above 1 (or at `auto` on a run big enough; see above), or from the command line, models are backtested at the same time on separate worker processes. From the command line, CUDA is selected automatically when a usable GPU is available, and one GPU-backed worker slot per visible CUDA device is added to the CPU worker capacity; the simulator's history runs use the CPUs only. Each run gives every shared file a single writer, so runs never collide:

| Role | How many | Reads | Writes |
|---|---|---|---|
| **Candle writer** thread | 1 | Coinbase | `data/candles.db`: it downloads each market's missing candles before any model on that market starts, then stores the slow indicator columns the workers computed. |
| **Workers** (processes) | as set | `data/candles.db`, read-only | Only their own shard: one small CSV per model under `data/performance_shards/<run>/`. |
| **Gatherer** thread | 1 | the shards | `data/model_performance.csv`: it merges shards as they finish, then deletes them. |

Workers are processes rather than threads because the backtest loop is plain Python, which threads would run one at a time. Each worker reads a market's candles once and reuses them for every model it runs on that market. Each worker holds its own copy of those candles, so memory grows with the worker count.

The results are the same as a one-after-another run. Parallel runs pay off with many models: each worker spends a second or two starting up and reading candles, so 8 models took 5 s on 4 workers against 10 s one after another, and the gap grows with more models. A model that fails is left out of the results, and the status line names it and the error. The other models still finish and are logged.

The same runs work without the window:

```bash
python parallel_backtest.py config/btc_15m_*.json                # auto: one worker per CPU but two
python parallel_backtest.py config/btc_15m_*.json --workers 4
python parallel_backtest.py config/*.json --start 2021-01-01 --fresh-signal
python parallel_backtest.py config/*.json --offline --no-log     # cached candles, fallback fees, no log
python parallel_backtest.py config/ETH-USD/eth_*.json --compute-device cuda --workers 4
python parallel_backtest.py --gather                             # merge shards a failed merge left behind
```

`--workers` takes `auto` (the default) or a number, lowered to one per CPU but two; CUDA adds one worker slot per visible device. The CPU count is the logical cores Python may use (`os.process_cpu_count()`), so on a processor with hyper-threading it is twice the physical cores. A run never starts more workers than it has models. It prints a table of each model's return, buy and hold, maximum drawdown and trade count. Without `--offline`, it needs the same `.env` key as the window.

CUDA is optional. Install `requirements-gpu.txt`; parallel simulations/backtests and model search use CUDA automatically when the optional dependencies and a visible GPU are available. `--compute-device cpu` forces CPU-only execution, while `--compute-device cuda` requires CUDA and reports an error if it is unavailable. CUDA adds one worker slot per visible device alongside the CPU worker capacity. Supported SMA, EMA, WMA, DEMA, TEMA and HMA indicators run on the GPU; calculations return to the CPU before rule evaluation and the stateful trading simulation, so fills, stops, fees and monthly tax-reserve handling remain CPU-based. Other indicators also continue to use the CPU. GPU worker processes still run the stateful simulation on CPU, and data transfers can outweigh indicator offload; GPU slots may not improve throughput when CPU simulation is the bottleneck, so benchmark representative profiles and candle ranges.

**Several runs at once.** The database is kept in WAL mode, so a reader never waits for a writer, and every connection waits up to 60 seconds for another's write to finish. You can therefore run the window, a command-line run and `model_search.py` at the same time; their downloads queue up rather than fail. The performance log is protected by a lock file; see [Model performance log](#model-performance-log).

---

## 6. Historical data and the cache

Each run covers the **last 10 years** up to the most recent closed candle, or as much history as the product has. The current, still-open candle is never used. (The 10-year window is `LOOKBACK_YEARS` in `backtest.py`.)

Candles are cached in a SQLite database, `data/candles.db`, with one table per product and candle size:

```text
data/candles.db
  "BTC-USD_FIFTEEN_MINUTE"   a row per candle: start, open, high, low, close, volume,
                             then a column for each stored metric (see below)
  candle_tables              the time range already downloaded for each table
  metric_columns             the candles each stored metric has been computed for
```

- **The first run** for a product and candle size downloads everything, in requests of up to 350 candles with a 0.1 second pause between them.
- **Later runs** load the cache and fetch only candles that have closed since the last run. New candles are added to the table, and candles already in it are never rewritten. The **Data gathering** bar says which happened: `candles loaded from cache`, `candles downloaded`, or a count of each.
- **Candles are kept as they traded.** Nothing is removed or smoothed, including flash crashes and prints that look like errors.
- **To force a fresh download**, clear the market with **Clear data...** (see [Clearing stored data](#clearing-stored-data)), or close the simulator and delete `data/candles.db`. Either way its stored metrics go too.
- **A CSV cache from an earlier version** (`data/BTC-USD_FIFTEEN_MINUTE.csv` with its `.meta.json`) is imported into the database the first time it is needed, which takes a couple of seconds. After that, the CSV files are not read, and you can delete them.
- `data/` is ignored by Git. It also holds `model_performance.csv`, the [model performance log](#model-performance-log).

Ten years of 15-minute BTC-USD candles take about 19 MB, and load in about half a second. To inspect the candles and stored metrics, open `candles.db` in any SQLite browser, such as DB Browser for SQLite.

### Synthetic candles

Synthetic candles are kept apart from the price history, in a database of their own: `data/synthetic/synthetic.db`. `data/candles.db` only ever holds candles downloaded from Coinbase, and its indicator columns only values computed on them.

- Every scenario path a synthetic or stress run builds is stored in its own table, `<product>_<candle size>__<key>_p<path>`, such as `BTC-USD_FIFTEEN_MINUTE__39244a87ce73_p0`. The key is a hash of the run's scenario modules and of the history it was built on.
- A table holds only the candles the run made or changed: for a run appended after history, its own candles; for one written in at a date, every candle from that date on (the ones after the run are the real history, shifted to continue from where the run ended). The history before them is the real one, so it isn't copied.
- `synthetic_runs` describes every table: the product and candle size, the run's name and path, its modules as JSON, the history it was built on (first and last candle, and how many), the candles stored and when.
- Building is seeded, so the same run on the same history makes the same candles; a table already stored is left as it is. A run on newer history gets a new table.
- Synthetic candles are marked as such while they are tested. The candle store refuses to save them, and they never use the stored indicator columns, which share the real candles' start times; their indicators are computed in memory.
- Clear them with **Clear data...**, or delete `data/synthetic/` while the simulator is closed, to free the space; nothing reads them back.

### Clearing stored data

**Clear data...** (beside **Test Coinbase Authentication**) opens a dialog listing the price history one market per row: the product, candle size, number of candles, the dates they run from and to, and how many indicator columns are stored with them. Below the list are the totals and the database's size on disk.

- **Clear selected markets** deletes the highlighted markets' candles and stored indicators (Ctrl-click or Shift-click to highlight several). **Clear all price history** deletes every market's.
- **Clear synthetic candles** deletes every stored scenario path (see [Synthetic candles](#synthetic-candles)).
- Each asks first, naming what it deletes; **No** keeps everything. Nothing is cleared while a simulation is running.
- A cleared market is downloaded again from Coinbase by the next run that needs it: ten years of 15-minute candles take several minutes. A CSV cache an earlier version left for it (`data/<market>.csv` with its `.meta.json`) is deleted too, so the fresh download isn't replaced by the old file.
- The database is compacted afterwards, giving the space back. Model profiles, the performance log and stress reports are never touched.

### Stored metrics

The models in a run share their indicators. An indicator that two profiles define the same way, such as a 100-day SMA, is computed once, whatever each profile names it. An indicator that is slow to compute is also kept between runs, as a column of the candle table.

- **Which indicators are stored.** An indicator is stored only when both of these hold:
  - Computing it takes longer than reading it back would: more than 1 microsecond per candle, about twice the cost of a read. The threshold is `STORE_SECONDS_PER_CANDLE` in `candle_store.py`.
  - Its value depends only on a fixed number of recent candles.

  In practice, this means CCI, and Donchian, Williams %R, Stochastic, WMA and HMA over long periods. For example, a 30-day CCI on 15-minute candles takes about 5.5 seconds to compute.

  Fast indicators, such as SMA and Bollinger Bands, are computed every run, because computing them is quicker than reading them back. EMA, DEMA, TEMA, MACD, RSI, ATR, ADX, Keltner, OBV and VWAP are never stored. Each of their values depends on the entire history before it, so new candles would mean recomputing them from the start. For the same reason, an indicator whose `source` is one of these is never stored either.
- **Column names.** A column is named after the indicator's type and settings, with periods counted in candles. For example, a 30-day CCI on 15-minute candles is stored as `CCI(period=2880)`. Each output of a multi-output indicator has its own column, ending in the output's name, such as `.upper`.
- **Filling in missing candles.** New candles have empty metric columns until a model needs that metric again. The next run that needs it reads the stored values back and computes only the missing candles, plus the earlier candles the indicator depends on. The `metric_columns` table records the candles each column has been computed for. Within that range, an empty cell is a warm-up candle where the indicator has no value yet; outside it, the cell has not been computed.
- **Results don't change.** Stored values match a fresh computation to within floating-point rounding. Before it fills in missing candles, a run recomputes the last 50 stored values and compares them. If any differ, the whole column is recomputed.

On 10 years of 15-minute BTC-USD candles, a profile with a 30-day CCI takes about 6.8 seconds per run without stored metrics, and about 1.4 seconds the day after its first run.

The amount of data depends heavily on the candle size:

| `period_granularity` | Candles in 10 years | Requests for a first download |
|---|---|---|
| `ONE_DAY` | about 3,650 | about 11 |
| `SIX_HOUR` | about 14,600 | about 42 |
| `TWO_HOUR` | about 43,800 | about 125 |
| `ONE_HOUR` | about 87,700 | about 250 |
| `THIRTY_MINUTE` | about 175,000 | about 500 |
| `FIFTEEN_MINUTE` | about 351,000 | about 1,000 |
| `FIVE_MINUTE` | about 1.05 million | about 3,000 |
| `ONE_MINUTE` | about 5.26 million | about 15,000 |

A 15-minute history takes several minutes the first time. One-minute data takes far longer, and it also makes every simulation slower and the charts heavier.

All times are in UTC.

---

## 7. Reading the results

### Comparison

The Comparison tab has one row per model, sorted by return with the best first. A line above the table gives the number of models and the best one, and, with a start date, when the test started. The table scrolls sideways and up and down.

**Sorting and filtering, as in Excel.** Every heading shows a ▾; click it to open its drop-down:

- **Sort smallest to largest** / **Sort largest to smallest** (for text columns, **Sort A to Z** / **Sort Z to A**) sorts the table by the column.
- **Number filter** (number columns only): pick **greater than**, **at least**, **less than**, **at most**, **equals** or **between** and type the number, or both numbers for between. Percent columns (Return, Buy & hold, vs Buy & hold, Max drawdown, Worst in window, Win rate) take the number in percent, as shown: greater than `100` keeps returns above +100%.
- **The checklist** lists the column's values, smallest first or A to Z, as shown in the table, all checked. Untick values to hide their rows, or untick **(Select All)** and tick the few to keep. It offers only the values left by the other columns' filters.
- **Search** narrows the checklist to values containing every word typed; with a search, **OK** keeps only the checked values the search shows, as in Excel.
- **OK** applies the drop-down (**Enter** too); **Cancel** (or **Esc**) leaves the column as it was; **Clear filter from "<column>"** removes its filter.

A filtered column's heading shows `[Name] ▼`. Rows must pass every column's filter; the line above the table counts them, such as `Showing 12 of 300 models`, and **Clear all filters** removes every filter. Sorting keeps the filters.

To chart some of the models, select their rows (Ctrl-click or Shift-click for several) and click **Chart selected models** above the table, or press **Enter**. The charts then show only those models (see [Showing model](#showing-model)), with the selected row highest in the table on show. With several models selected, the **Model value** tab opens; with one, the **Chart** tab. Rows not in the charts' view are shown in gray. Double-click a model to open its chart; a gray model is added to the view.

| Column | Meaning |
|---|---|
| Model, Product, Candles | The file name, the `product_id` and the candle size. |
| Start, End | The starting capital and the ending value. |
| Return | The ending value divided by the starting capital, minus 1. |
| First buy | When the model first bought on its entry signal, or `never`. |
| B&H start | Where this model's buy-and-hold benchmark starts: the first candle on which the model could buy, once its indicators have warmed up, or its first buy if that came sooner. Each model has its own, so check it when comparing models. |
| Buy & hold | The return from buying at **B&H start** and holding to the end: the last close divided by the close at B&H start, minus 1. It includes no fees or taxes. See [Buy and hold starts when the model can first buy](#buy-and-hold-starts-when-the-model-can-first-buy). |
| vs Buy & hold | How much bigger (+) or smaller (−) the model's ending value is than buy and hold's: the model's growth multiple divided by buy and hold's, minus 1. For example, 90× against 60× is +50%, and 50× against 60× is −16.7%. The Metrics tab shows the same figure as **Model vs buy and hold**. |
| Max drawdown | The largest fall in the model's value from a previous peak. |
| Worst in window | The largest fall from the highest value within the trailing window (`safeguards.drawdown_window`, or 180 days when it is not set), shown with the window, such as `12.34% / 180d`. |
| Flash buys | The number of flash-crash limit buys that filled and were sold when their crash recovered. Buys still held at the end are not counted. |
| Trades, Win rate | Completed trades, flash-crash buys included, and the share of them with a positive profit after fees. |
| Fees, Taxes | Total commissions paid and tax withheld, including any reserve awaiting the next monthly payment. |
| Blocked signals | Entry signals the [friction screen](model_configuration.md#5-the-friction-screen-worked-through) rejected. |

### Chart

The Chart tab draws the chosen model on a log price scale:

- the close price, with every price-scale indicator (moving averages, VWAP, band and channel lines)
- a separate panel under the price for each oscillator (RSI, MACD, ADX and so on), with its conventional reference levels
- a thin green line through the chart at each buy and a red one at each sell, with a ▲ (buy) or ▼ (sell) in a strip kept clear along the bottom edge, so the marks never cover the price or indicator lines (with a price floor set, the strip isn't reserved)
- gray shading where trading was paused by the drawdown limit
- for a model with [regimes](model_configuration.md#49-regimes-experimental), which regime it was trading in (see [Regime marks](#regime-marks))

The subtitle gives the model's trade count and return. Hover anywhere to see a crosshair and a tooltip listing the close and every indicator's value at that candle (and the active regime, for a model with regimes); an indicator still in its warm-up shows `warming up`. Use the toolbar to zoom, pan, go back, or save the chart as an image. All panels share one time axis, so zooming one zooms them all.

**Lowest price shown** sets a floor for the price axis. Type a price and press **Enter** or **Apply**; **Clear** fits the axis to the data again. Lines below the floor are cut off, which is useful when one stray price squashes the chart: BTC-USD has a bad $0.06 print on 2017-04-15 that drags the 100-day Donchian channel down for 100 days, and a floor of `500` removes it from view. The floor:

- accepts a plain number, thousands separators or a leading `$`, such as `1,000` or `$500`, and must be above zero because the axis is a log scale
- applies to the price chart only; oscillator panels keep their own scales
- stays in place when you pick another model or run again
- redraws the full view, which becomes what the toolbar's Home button returns to
- also applies when you double-click a trade, but never hides that trade's own buy and sell markers

If the floor is above every price on the chart, the note beside it says so and the chart shows all its values. If the text isn't a number above zero, the note says why and the floor is left as it was.

For how indicators are drawn and how to hide some, see [Charts and tooltips](indicator_reference.md#10-charts-and-tooltips).

#### Regime marks

A model with [regimes](model_configuration.md#49-regimes-experimental) switches its rules with the market's situation. The Chart tab, and the Model value tab when it shows one model, mark which regime was in use:

- a colored **strip along the top** of the chart, one color per regime and gray for the base regime, with the regime's name wherever its stretch is wide enough to fit it. The legend names every regime's color.
- a faint wash of the regime's color over every panel while it is active (none for the base regime)
- a dashed line in the new regime's color at each **switch**

Nothing is marked while the model's indicators warm up: the regime can't be told until every detector is ready, and the tooltip shows `warming up` there. Hover anywhere else to read the active regime. The Metrics tab lists each regime's detector, rules and stops, the time spent in each, and the latest switches.

### Model value

The Model value tab draws the value over time of every model in view on one chart: after a run, every model, or the 8 best by return when there are more (see [Showing model](#showing-model)). A second panel below it tracks each model's tax reserve, which builds as profitable trades close and returns to zero at each monthly payment.

**Models on the chart**, the list on the left, picks which models it draws. It lists every model of the run, best return first, with its return:

- **Click** a model to chart it alone. **Ctrl-click** adds or removes one, and **Shift-click** selects a range. The chart redraws a moment after your last click, so several quick clicks redraw it once.
- Models on the chart are highlighted, each name in its line color, so the list is also a key to the chart. A model keeps its color while it stays on the chart; one that joins takes a free color.
- **Search** narrows the list to names containing every word you type. Models the search hides stay on the chart.
- **Best 8** charts the 8 best models by return. **All listed** adds every listed model, and **Drop listed** removes them; with a search, they act on its matches only.
- The line under the buttons counts the models on the chart. Above 8, colors repeat as dashed lines, and the line warns that the chart is getting hard to read. At least one model always stays on the chart.

The chart has these reference lines:

- a horizontal line at the starting capital, one for each different starting capital
- a gray **buy and hold** line for each product and starting capital. It stays at the starting capital until the model could first buy, then follows the price from that candle's close. An open circle marks where it starts, and the legend gives the date. With several models, it uses the start of the model chosen in **Showing model** (or, for another product or starting capital, of the first model with that product and starting capital), and the legend names that model.

With one model, the chart labels the start and end values, the subtitle gives the model's return and the buy-and-hold return with its start date, gray shading marks drawdown pauses, and a model with regimes gets the same [regime marks](#regime-marks) as the Chart tab. With several models, the legend shows each model's ending value and return, the subtitle names the best model, and the model chosen in **Showing model** is drawn slightly heavier. Hover to read every model's value, tax reserve and buy-and-hold value at one moment, each with its return where applicable.

**Lowest value shown** works like the Chart tab's floor, in value units. For example, set it to the starting capital to hide the part of the chart below it. Each tab keeps its own floor.

### Trades

The Trades tab lists every completed trade of the chosen model, flash-crash buys included, with a summary above the table:

- wins and losses
- net P&L after fees and per-trade tax reserve
- average, best and worst return
- a count of each exit reason

Click a column heading to sort by it, and click again to reverse. Double-click a trade to zoom the chart to it, with some time on each side. The trade's holding period is shaded and its markers are drawn larger, and unbounded oscillator panels rescale to the zoomed values. The toolbar's back button returns to the previous view.

| Column | Meaning |
|---|---|
| # | The trade's number, in order of exit. |
| Entry, Exit (UTC) and prices | Fill times and fill prices, including slippage. |
| Held | How long the position was open, such as `3d 4h` or `2h 15m`. |
| Position | The cash committed to the trade, including the entry fee. |
| Fees | The fees for both ends of the trade. |
| Net P&L, Return | The profit after fees and per-trade tax reserve, in currency and as a fraction of Position. |
| Tax reserved | The amount set aside from this trade's positive net gain. |
| Outcome | `Win` if the P&L is positive, otherwise `Loss`. |
| Value after | The model's total value after the exit. |
| Exit reason | Why the trade closed (see [Exit reasons](#exit-reasons)). |

### Metrics

The Metrics tab holds the full text report for the chosen model, headed by its name:

- the product, the period and the number of candles simulated; with a start date, also the test start, the days of warm-up and the first entry
- headline results: starting capital, ending equity and total return; the buy-and-hold return with where it starts; **Model vs buy and hold**; without a start date, **First buy**; and, when the two differ, **Buy and hold from the test start**
- the maximum drawdown, the worst drawdown within the window, trades and win rate, fees paid with the rate per fill, and whether a position is open at the end
- the indicators and rules
- the **friction model** actually used: fee rates and their source, slippage, the liquidity buffer, tax, the friction-screen verdict and **Entry signals blocked by the screen**
- the tax paid each month and the reserve still held for the unfinished final month
- flash-crash events, each with its low and how long it took to recover, and the last 10 flash-crash fills
- drawdown pauses, with when each resumed
- the last 10 trades
- the settings the simulator does not model yet, under **Not modeled yet**

**Load Profile** and **Test Coinbase Authentication** also write their reports here.

### Model performance log

Every run also records each model's results in `data/model_performance.csv`, one row per model, so the performance of every model you create and test builds up in one place. Open it in Excel or any spreadsheet.

- **A model tested for the first time** gets a new row.
- **A run whose results differ** from the model's row replaces that row. This includes a different start date, newer candles, a different fee tier, or an edited profile.
- **A run with identical results** leaves the file untouched.
- The status line at the bottom of the window reports what happened, such as `model_performance.csv: 1 added, 2 updated, 3 unchanged`.

Rows are keyed by the model's name, which is its file name, and sorted by it. The columns are:

| Column | Meaning |
|---|---|
| `model`, `product`, `candles` | The profile's file name, `product_id` and candle size. |
| `profile_fingerprint` | A short hash of the profile's contents. It changes whenever you edit the profile, so a row shows which version of the model produced it. |
| `test_start`, `test_end`, `requested_start`, `wait_for_fresh_signal` | The test window (UTC, `YYYY-MM-DD HH:MM`) and the run settings. `requested_start` is blank when no start date was set; `wait_for_fresh_signal` is `yes` or `no`. |
| `commission_rate_pct` | The fee per fill, in percent, that the run used. |
| `starting_capital`, `ending_value`, `growth_multiple`, `return_pct` | The model's result. A growth multiple of 50 means 50 times the starting capital. |
| `first_buy`, `buy_and_hold_start` | When the model first bought on its entry signal (blank if never), and where its buy-and-hold benchmark starts. |
| `buy_and_hold_multiple`, `buy_and_hold_return_pct` | The benchmark's result from `buy_and_hold_start`. |
| `vs_buy_and_hold_pct` | How much bigger (+) or smaller (−) the model's ending value is than buy and hold's, in percent. |
| `max_drawdown_pct`, `worst_in_window_pct`, `drawdown_window` | The drawdowns, as in the Comparison tab. |
| `trades`, `win_rate_pct`, `fees`, `taxes`, `blocked_signals`, `flash_buys` | Trading activity and costs; `taxes` includes both paid tax and any reserve still held for the next monthly payment. |
| `first_recorded_utc`, `last_changed_utc` | When the model's row was first written, and when its results last changed, such as `2026-10-14 09:30:00 UTC`. These two are not compared when deciding whether a run changed anything. |

Numbers are plain values without thousands separators or % signs, so they sort and chart correctly. If the file is open in Excel, Windows locks it: the run still completes and shows its results, but the status line says the file was not updated. Close it and run again to record the results.

A few more details:

- **The file is never half written.** Each update is written to `model_performance.csv.tmp` and then swapped into place.
- **Simultaneous runs never lose rows.** Every update holds `model_performance.csv.lock` while it reads, merges and replaces the file, so the window, `parallel_backtest.py` and `model_search.py` can record at the same time. A lock older than two minutes is left over from a crashed run, and the next update takes it over.
- **Parallel runs keep their results until they are merged.** If the file is locked by Excel when a [parallel run](#running-models-in-parallel) finishes, the status line says how many results were kept in `data/performance_shards/`. Close the file and run `python parallel_backtest.py --gather` to merge them.
- **Models that aren't in a run are kept.** Running a subset of your models updates only their rows.
- **Renaming a profile file starts a new row**, because rows are keyed by the name. Delete the old row by hand if you no longer want it.
- **Files from an earlier version** with fewer columns are upgraded in place: the next run of a model fills in the new columns for its row, and the other rows keep them blank until their model is run again.
- **Comparing results between machines or dates:** check `profile_fingerprint`, `test_end` and `commission_rate_pct` first. Two rows with different values in any of them were not tested under the same conditions.

---

## 8. How trades are simulated

The backtest (`backtest.py`) is a long-only, spot simulation. It holds at most one strategy position at a time and uses no leverage or shorting. It processes each candle in order and never looks ahead: a rule evaluated at a candle's close sees only that candle and earlier ones.

### Order of events in each candle

1. **New UTC day:** the `max_orders_per_day` count resets.
2. **New calendar month:** pay the prior month's segregated tax reserve (see [Taxes](#taxes)).
3. **Flash crashes**, if `flash_crash_buy` is set: any resting limit buy the candle traded down to fills, each level once per crash. What those buys hold is sold at the close of the candle in which the crash recovers.
4. **Exits**, if a position is open, checked in priority order:
   1. **Stops.** These are the hard stop, the trailing stop and the drawdown stop. If the candle's low reaches any of them, the highest one triggered fills.
   2. **Profit lock.** If the candle's high reaches the target, the position sells at the target.
   3. **Exit rules.** If the exit rules hold at the close, the position sells at the close.

   When a stop and the target are both reached within one candle, the stop is assumed to come first, which is the conservative choice. During a flash crash, nothing is sold until the crash recovers (see [Flash crashes](#flash-crashes)).
5. **Drawdown limit** (the mode without a window): if the model's value at the close has fallen `max_drawdown_limit` below its peak, the position is sold at the close and trading pauses.
6. **Entries**, if no position is open. The model buys at the close when all of these hold:
   - the entry rules hold
   - trading isn't paused
   - with **Wait for a fresh entry signal** ticked, the signal has switched off at least once since the start
   - the daily order limit allows it
   - there is cash to spend beyond the flash-crash reserve
   - with a drawdown window, there is room above the limit for the trade's costs
   - the signal passes the friction screen. A signal the screen blocks is counted once, however many candles it stays true.

   A position closed by a stop or target earlier in the same candle can be reopened here if the entry rules still hold.

### Fills, fees and slippage

| Event | Fill price | Fee |
|---|---|---|
| Entry (`ENTRY_SIGNAL`) | close × (1 + slippage) | taker |
| Exit rule, drawdown limit | close × (1 - slippage) | taker |
| Stop | the stop price, or the open if the candle gapped below it, × (1 - slippage) | taker |
| Profit lock | the target price, or the open if the candle gapped above it, × (1 - slippage) | taker |
| Flash-crash buy | the level's limit price, even if the candle opened below it | maker |
| Flash-crash rebound sale | close × (1 - slippage) | taker |

- **Fees** are the rate × `friction_model.fee_buffer_multiplier` (1 when it is not set). Every fill except a flash-crash buy pays the taker rate, because it fills at once at a candle price.
- The **hard stop** sits at entry fill price × (1 - `hard_stop_percentage`). It needs `safeguards.use_hard_stop` set to `true`, and is off when `strategy.exit_signal.hard_stop_trigger` is `false`.
- The **profit target** sits at entry fill price × (1 + `profit_lock_target`).
- The **trailing stop** sits at the highest price since entry × (1 - `trailing_stop`). The highest price starts at the entry candle's close and takes in each later candle's high, as of the previous candle, because a candle's high and low can't be ordered within it.
- **Position size:** an entry spends (cash - flash-crash reserve) × `target_position_fraction`. The fee comes out of that amount.
- **Flash-crash buy size:** each level spends its `allocation` of the `reserve`, or, without a reserve, its `allocation` of the cash available when it fills.

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

Each completed trade with a positive net gain reserves `tax_buffer_rate` of that gain from trading cash. Losses do not offset tax reserved on other trades. The segregated reserve is paid at the first candle of the next UTC month, and the Tax reserve panel under Model value drops to zero at that point. Trading equity is reduced when tax is withheld, not again when the reserve is paid, and no holdings are sold to make the payment. An unfinished month's reserve remains withheld at the end of the test.

### Drawdown limit

`safeguards.max_drawdown_limit` works in one of two modes, depending on whether `drawdown_window` is set. After either kind of drawdown exit, trading resumes only when the entry signal switches off and then fires again. For details, see [The two drawdown modes](model_configuration.md#the-two-drawdown-modes).

### Flash crashes

Crashes are never removed from the data, so the strategy sees every crash's real prices, and they count in the model's value and drawdowns.

With `strategy.flash_crash_buy`, the limit buys catch the dip and the model holds through the crash:

- **A crash starts** on a candle whose low is at or below the shallowest level's `drop` under the previous candle's close. It **recovers** at the first close back above that level.
- **Each level** is a limit buy at its `drop` below that same close.
- **Nothing is sold during the crash.** Stops and the profit lock don't fire on any crash candle, including the one it recovers in. Exit rules and the drawdown limit resume at the recovery candle's close.
- **The flash-crash buys are sold** at the recovery candle's close. If the crash never recovers, they are still held at the end and valued at the last close.
- **The Metrics tab** lists each crash in the test with its low and how long it took to recover, or `never recovered`.

Without `flash_crash_buy`, a crash is an ordinary candle. A stop it reaches fills at the stop price, or at the open if the candle opened below it, however far down that is.

For the settings, see [flash_crash_buy](model_configuration.md#flash_crash_buy).

### Start date and warm-up

Indicators are computed over the whole downloaded history, so the history before your start date warms them up and they are fully formed on the first day of the test.

From the start date on:

- each model holds its starting capital in cash and waits for its own entry rules
- returns, drawdown, taxes and the charts are all measured from the start date
- buy and hold is measured from the first candle the model could buy on (see below), which is the start date itself once the history before it has warmed the indicators up

The Metrics tab reports:

- the actual start
- how many days of warm-up were available
- when the first entry came

If the date is before the first available candle, the test starts at the first candle, and the Metrics tab says so when the date is more than a day earlier. A date after the last candle stops the run with an error.

Without a start date, the test begins at the first candle. Each indicator then has no value until it has enough history, and no rule can fire during that warm-up, so the model sits in cash.

### Buy and hold starts when the model can first buy

The buy-and-hold benchmark buys at the close of the first candle on which the model *could* buy: the first candle where every entry rule can be evaluated, because every indicator it reads has warmed up and every lookback in it (such as `change(sma_50, '10d')` or `highest(close, '100d')`) has enough history. It then holds to the end.

- **The warm-up doesn't count against the model.** Without a start date, a model with a 200-day indicator can't trade for its first 200 days. Buy and hold from the first candle would grow through that time while the model couldn't act.
- **Waiting after the warm-up does count.** A model that is ready but chooses to stay in cash, for example through a bear market, is credited or penalized for that choice, because buy and hold is already invested.
- **With a start date** after the warm-up, buy and hold starts on the start date itself.
- **A first buy before that candle** (possible when an `or` rule fires before all of its parts have warmed up) moves the start back to the first buy.
- **A model whose entry rules never become ready, and that never buys,** is compared from the test start.
- **Each model has its own start.** The Comparison tab shows it as **B&H start** next to **First buy**, the Metrics tab states it, and the Model value chart marks it with an open circle.
- **The Metrics tab also reports Buy and hold from the test start**, for reference, when the two differ.

### What the results do not include

Backtests flatter strategies. Keep these limits in mind:

- **Fills are idealized.** Orders fill at candle prices, plus your slippage setting, whatever their size.
- **Today's fee tier is applied to the whole history.**
- **Taxes use one flat rate** on each profitable trade's net realized gain after fees. Losses do not offset another trade's reserve; short-term and long-term rates are not distinguished.
- **An open position at the end is valued at the last close**, without selling costs, and so are flash-crash buys still held. Tax already reserved from profitable trades is excluded from ending equity, including any reserve for the unfinished final month.
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

- **The cache must already exist.** Run the simulator once for that product and candle size first. `load_cached_candles` returns `None` when there is no cache, and the script above then stops with a `TypeError`.
- **Without live inputs**, `FrictionModel(profile)` uses the profile's fallback fee rate and no Bitcoin network fee. The report shows this as `profile fallback (live rates not fetched)`.

The candle list that `load_cached_candles` and `load_candles` return carries its market's metric cache. Backtests run on that same list share their indicators and use the [stored metrics](#stored-metrics), as the simulator's runs do. A sweep should therefore load the candles once and pass the same list to every `run_backtest` call. A plain list of candle dictionaries works too, but each backtest then computes all of its indicators itself.

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
| `load_candles(client, product_id, granularity, start, end, cache_dir=..., progress=None)` | `backtest` | Cached download of candles in the range `[start, end)`. The list carries the market's metric cache. |
| `load_cached_candles(cache_dir, product_id, granularity)` | `backtest` | Reads the cache only. Returns `(candles, fetched_start, fetched_end)` or `None`. |
| `lookback_window(granularity_seconds)` | `backtest` | The 10-year `(start, end)` the simulator uses. |
| `parse_start_date("2022-01-01")` | `simulator` | Converts a date string to the `start_time` timestamp. |
| `fetch_friction_inputs(client, profiles)` | `friction` | Looks up the live fee tier and Bitcoin network fee that one profile, or a list of them, needs. A failed lookup is recorded, not raised. |
| `update_performance_log([(result, profile), ...], path=...)` | `performance_log` | Records results in the [model performance log](#model-performance-log), as a run does. Set `result["model_name"]` first, or the row is named after the product. Returns the model names `added`, `updated` and `unchanged`. |
| `describe_update(outcome, path=...)` | `performance_log` | The status-line summary of an update, such as `model_performance.csv: 1 added`. |

The main keys of the result dictionary are:

- `total_return`, `ending_equity` and `starting_capital`
- `period_start`, `period_end` and `candle_count`, the test window; `requested_start` and `warmup_candles` describe the start date
- `first_entry`, the time of the model's first buy on its entry signal (`None` if it never bought)
- `ready_to_trade`, the first candle on which every entry rule could be evaluated (`None` if never)
- `buy_and_hold_return`, measured from `buy_and_hold_start` (`ready_to_trade`, or the first buy if that came sooner), and `buy_and_hold_from_test_start`
- `max_drawdown`, `max_window_drawdown` and `drawdown_window`
- `trade_count`, `win_rate`, `fees_paid`, `commission_rate`, `taxes_paid`, `taxes_reserved` and `screened_signals`
- `tax_payments`, one per month boundary, and `estimated_tax_due` for the unfinished final month's reserve
- `trades`, a list of dictionaries with entry and exit times, prices, pre-tax `pnl`, `tax_withheld`, after-tax `net_pnl`, fees and reasons
- `flash_crash_buys` (the sold flash-crash buys), `flash_crash_events` and `open_flash_lots`
- `open_position` and `drawdown_pauses`
- `friction`, which describes the rates used
- `series`, the time, close, equity, tax-reserve and indicator arrays behind the charts

All times are Unix timestamps in seconds, UTC.

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Select at least one model in the Models list` | Nothing is selected. Click a model; if the list is empty, add a profile to `config/` and click **Refresh**. |
| `Fix these profiles first ...` | A selected profile is invalid. Click **Load Profile** to see every problem, then see [Validation and troubleshooting](indicator_reference.md#8-validation-and-troubleshooting). |
| `Start date '...' must look like 2020-01-01 or 2020-01-01 14:30 (UTC)` | The start date isn't in a format the simulator reads. Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`, or leave it blank. |
| `Start date ... is in the future` | Choose a date up to now, in UTC. |
| `Coinbase API key name and private key are required ...` | `.env` wasn't found or is incomplete. Run the simulator from the repository root, and check `.env` (see [trader.md](trader.md#2-setting-up-the-env-file)). |
| `Coinbase key file not found: ...` | `COINBASE_KEY_FILE` points to a file that doesn't exist. A relative path is read from the current directory, so run the simulator from the repository root. |
| `Coinbase API test failed: HTTP Error 401 ...` | The key name or private key is wrong, or the key has expired. If the key has an IP allowlist, add the outbound IP address shown in the message. |
| `... HTTP Error 403 ...` | The key works but lacks permission. Enable **View** on the key. |
| `Could not reach the Coinbase API` | A network problem, or a wrong `COINBASE_BASE_URL`. |
| A dialog asks to install the Coinbase SDK | `coinbase-advanced-py` isn't installed in the Python running the simulator. Accept, or run `pip install -r requirements.txt` in your virtual environment. |
| `The coinbase-advanced-py package is unavailable to ...` | You declined the install, or it failed. Run `pip install -r requirements.txt` with the Python named in the message. |
| A dialog asks to add Python to `PATH` | The running Python's folder isn't on `PATH`. Declining is harmless when the right interpreter is already running. Accepting adds the folder to your user `PATH`, persistently on Windows. |
| `Only LONG_ONLY strategies can be simulated so far` | Set `strategy.direction` to `LONG_ONLY` or remove it. |
| `The start date ... is after the last candle` | Choose an earlier date. |
| `No candles to simulate` | Coinbase returned no candles for the product in the 10-year window. Check `product_id`. |
| The note beside **Lowest price shown** says `Floor ... must be a number` | Type a plain number above zero, such as `500`, or click **Clear**. |
| The run finishes with no trades | Check **Entry signals blocked by the screen** in Metrics first (see the [friction screen](model_configuration.md#5-the-friction-screen-worked-through)). For more checks, see the [no-trades checklist](indicator_reference.md#8-validation-and-troubleshooting). |
| Fees show `profile fallback (...)` | The live fee lookup failed (the reason is shown), so the profile's fallback rate was used. |
| The first run is slow | It is downloading the full history; later runs use the cache. Larger candle sizes download much faster (see [section 6](#6-historical-data-and-the-cache)). |
| Status line ends with `model_performance.csv not updated (...)` | The file is open in another program, usually Excel, which locks it. The results on screen are complete. Close the file and run again to record them. |
| Buy & hold differs between models in one run | Expected: each model's benchmark starts when that model could first buy. Compare the **B&H start** column (see [Buy and hold starts when the model can first buy](#buy-and-hold-starts-when-the-model-can-first-buy)). |
| The chart is crowded | Hide less important indicators with `"plot": false`, or enlarge the window. |

# Configurable Crypto Trading Bot

A local-first toolkit for designing, backtesting and eventually running cryptocurrency trading strategies on Coinbase Advanced Trade.

Each strategy is a JSON **model profile**. A profile names its indicators, writes its entry and exit rules as formulas, and sets its costs, capital and safeguards. To change a strategy, you edit its profile; no code changes are needed.

```json
"indicators": {
  "fast_ema": {"type": "EMA", "period": "12h"},
  "slow_ema": {"type": "EMA", "period": "3d"},
  "rsi":      {"type": "RSI", "period": 14}
},
"strategy": {
  "entry_signal": {"all": ["crosses_above(fast_ema, slow_ema)", "rsi < 70"]},
  "exit_signal":  {"any": ["crosses_below(fast_ema, slow_ema)"], "profit_lock_target": 0.08}
}
```

> **Not financial advice.** This is experimental software. Backtest results are not predictions, and the trader does not place orders yet.

---

## Contents

- [What's in the project](#whats-in-the-project)
- [Sneak peek: backtested models](#sneak-peek-backtested-models)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [How the pieces fit together](#how-the-pieces-fit-together)
- [The quant infrastructure blueprint](#the-quant-infrastructure-blueprint)
- [Project layout](#project-layout)
- [Configuration, secrets and private files](#configuration-secrets-and-private-files)
- [Running the tests](#running-the-tests)
- [Safety](#safety)

---

## What's in the project

| Tool | Command | What it does | Status |
|---|---|---|---|
| **Simulator** | `python simulator.py` | A desktop app that backtests one or more model profiles over up to 10 years of Coinbase candles. It charges fees, slippage and taxes, and shows the results in sortable tables and interactive charts. | Working |
| **Trader** | `python engine.py` | The headless runtime for an always-on machine. It loads profiles from `quant_system/config/` (one per ticker), opens its ledger and serial link, and starts a status dashboard with an on-demand Coinbase authentication check. Run directly, it exits as soon as start-up finishes; the [Trader guide](Project_Documentation/trader.md#keeping-the-engine-running) has a launcher that keeps it running. | Foundation only; does not trade yet |
| **Model profiles** | `config/*.json` | Declarative strategy definitions in one format for both tools (the simulator reads `config/`; `python engine.py` reads `quant_system/config/`): 24 indicators, a safe rule language, and settings for friction, capital and safeguards. | Working |
| **Model search** | `python model_search.py` | Breeds and backtests generations of vote-based models for one market, seeded by your existing models and their logged results, until improvement plateaus. Writes the best to `config/` and the performance log. Everything it searches over comes from a private settings file in `search_config/` ([README](search_config/README.md)). | Working |

---

## Sneak peek: backtested models

The project's own research has produced over 150 models, each backtested on 10 years of BTC-USD or ETH-USD history. Four of the **weaker** BTC models are published as templates in [config/.example/](config/.example/README.md#backtested-models-a-sneak-peek), so you can run them yourself and see what the simulator reports:

| Template | Idea | Return | Buy & hold | vs Buy & hold | Max drawdown |
|---|---|---|---|---|---|
| `btc_sma_30_100` | 30/100-day SMA trend, 20% positions, flash-crash buys, 10% drawdown stop | +52.8% | +8,636% | −98.3% | 16.5% |
| `btc_ema_10_100` | 10/100-day EMA crossover, all in | +4,113% | +8,636% | −51.8% | 71.0% |
| `btc_growth_09` | Two of three trend signals, including ADX | +6,950% | +8,659% | −19.5% | 63.5% |
| `btc_trend_vote_4in_1out` | Five-way trend vote: in on 4, out on 1 | +5,791% | +6,209% | −6.6% | 65.4% |

These figures come from one run on 2026-10-15 (UTC) over the previous 10 years, on 15-minute candles for the first two models and daily candles for the other two. It charged a 0.9% fee per fill (flash-crash limit buys pay the lower maker fee), 0.25% slippage, and 31.3% tax on each year's net realized gains, with losses carried forward. Buy and hold pays none of these costs, and starts when each model could first buy. All four finish behind it, which is why they're public. The better models stay private. The simulator also records every model you test in [a running performance log](Project_Documentation/simulator.md#model-performance-log), so comparisons like this one build up as you work.

---

## Quick start

These steps take you from a fresh clone to your first backtest.

1. **Install Python 3.11 or newer.** The project is developed and tested with Python 3.13. On Linux, also install tkinter for the simulator window: `sudo apt install python3-tk`.

2. **Create a virtual environment and install the requirements** from the repository root:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows; on macOS or Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Create a view-only Coinbase API key** and put it in `.env`. Every simulator run needs the key, even when the candles are already cached: it checks the key, looks up your fee tier, and downloads any new candles. It never trades.

   ```bash
   cp .env.example .env            # then fill in COINBASE_API_KEY_NAME and COINBASE_API_KEY_SECRET
   ```

   For step-by-step instructions, see [Creating a Coinbase API key](Project_Documentation/trader.md#3-creating-a-coinbase-api-key).

4. **Copy a template profile** into the private `config/` folder:

   ```bash
   cp config/.example/btc_macro_horizon.example.json config/btc_macro_horizon.json
   ```

5. **Run the simulator** from the repository root:

   ```bash
   python simulator.py
   ```

   Select the model and click **Run Simulation**. The run checks every selected profile first; click **Load Profile** to see the full validation report beforehand. Run the simulator from the repository root: it reads `.env` from the current folder, so starting it elsewhere means no API key. The first run downloads the price history, which can take several minutes. Later runs use the local cache.

From there, read the [Model configuration guide](Project_Documentation/model_configuration.md) to write your own strategies.

---

## Documentation

All documentation lives in [Project_Documentation/](Project_Documentation/), which has its own [index](Project_Documentation/README.md). Every tracked folder in the repository also has a `README.md` that explains what the folder holds (see [Project layout](#project-layout)).

### Guides: how to use the tools

| Page | What it covers |
|---|---|
| [Simulator guide](Project_Documentation/simulator.md) | Installing and launching the simulator; every control and results tab; the buy-and-hold benchmark; the model performance log; the candle cache; exactly how trades are filled, charged and taxed; what a backtest leaves out; scripting backtests in Python; troubleshooting. |
| [Trader guide](Project_Documentation/trader.md) | Every `.env` setting; creating a view-only Coinbase API key; running the engine and keeping it running; the status dashboard and its security; profiles, ledger, serial link and GPU helper; the trading safety switches; troubleshooting. |
| [Model configuration guide](Project_Documentation/model_configuration.md) | Creating a profile step by step; every field with its default and whether the simulator uses it; flash-crash buys; the friction screen with a worked example; what validation catches. This page was formerly `config_file_structure.md`. |

### References: for looking things up

| Page | What it covers |
|---|---|
| [Indicators and signal rules reference](Project_Documentation/indicator_reference.md) | The language profiles are written in: [defining indicators](Project_Documentation/indicator_reference.md#2-defining-indicators) with periods in candles or durations and their warm-up; the [catalog of all 24 indicators](Project_Documentation/indicator_reference.md#4-indicator-catalog) with formulas; the [rule syntax and functions](Project_Documentation/indicator_reference.md#5-writing-signal-rules); eight [strategy recipes](Project_Documentation/indicator_reference.md#7-strategy-recipes); [every validation message and its fix](Project_Documentation/indicator_reference.md#8-validation-and-troubleshooting); [migrating old-format profiles](Project_Documentation/indicator_reference.md#9-migrating-older-profiles); and how the charts draw indicators. |
| [Reference links](Project_Documentation/reference_links.md) | Links to the Coinbase Developer Platform and Kraken API documentation, for integration work on market data and order routing. |

### Design documents: why the project is built this way

| Page | What it covers |
|---|---|
| [Quant infrastructure blueprint](Project_Documentation/quant_infrastructure_blueprint.md) | The founding specification. Its five sections cover the workstation and headless-node architecture; the original engine code listing; storage and memory budgets; the LAN firewall rules; and the hardware cutover procedure. [Summarized below](#the-quant-infrastructure-blueprint), with what has been built so far. |
| [System configuration design](Project_Documentation/system_config_structure.md) | A planned shared configuration file for exchange connections, fee refresh schedules and global safeguards, kept separate from model profiles so that no model can grant itself trading access. It is not built yet; the local `.env` file holds these settings today. |

---

## How the pieces fit together

```text
  config/*.json           simulator.py                 Comparison, Chart, Model value,
  model profiles  ----->  10-year backtests  ------->  Trades and Metrics tabs
        ^                 on Coinbase history                    |
        |                                          data/model_performance.csv
        |                                          (one row per model tested)
        |                                                        |
        +------------ edit the profile, compare variants <-------+

  When a profile is ready:  copy it to the trader's config/ folder  ----->  engine.py
                            (live trading is not built yet)
```

The code is split into small modules:

| Module | Role |
|---|---|
| `simulator.py` | The simulator window: model selection, runs, results tabs. |
| `backtest.py` | Candle download, and the backtest itself: fills, stops, taxes, drawdown limits, flash crashes. |
| `candle_store.py` | The candle cache: a SQLite table per product and candle size, with a column for each slow indicator kept between runs. |
| `performance_log.py` | The model performance log: one CSV row per model with its latest results, updated when a run's results change. |
| `model_config.py` | Validates a profile and compiles its indicators and rules into per-candle signals. |
| `indicators.py` | The indicator library: moving averages, oscillators, bands, volatility, trend and volume. |
| `rules.py` | The rule language: formulas parsed against a whitelist and never executed as Python. |
| `friction.py` | Trading costs: live fee tier, slippage, Bitcoin network fee, tax, and the friction screen. |
| `chart.py` | The price, indicator and model-value charts, with hover tooltips. |
| `engine.py` | The trader: settings, profile loading, ledger, Coinbase authentication, status dashboard, serial link, GPU helper. |

---

## The quant infrastructure blueprint

[quant_infrastructure_blueprint.md](Project_Documentation/quant_infrastructure_blueprint.md) is the project's founding specification. It describes the complete system the code is building towards. This section explains its design and shows how much of it exists today.

The blueprint has five sections, which this summary follows:

| Blueprint section | Summarized under |
|---|---|
| 1. Architecture Overview & Structural Matrix | [Two machines, two jobs](#two-machines-two-jobs) and [Three cross-cutting principles](#three-cross-cutting-principles) |
| 2. Core Execution Engine & Hardware Interlock Blueprint | [Hardware and resource design](#hardware-and-resource-design). This section also holds the original `engine.py` listing and a template profile in the original format. |
| 3. Physical Storage Matrix & Memory Benchmarks | [Hardware and resource design](#hardware-and-resource-design) |
| 4. Production Security & Local LAN Isolation Rules | [Security and hardware cutover](#security-and-hardware-cutover) |
| 5. Execution Protocol for Staged Hardware Cutover | [Security and hardware cutover](#security-and-hardware-cutover) |

### Two machines, two jobs

*Blueprint section 1.* The blueprint separates heavy, bursty work from light, continuous work and gives each to its own machine.

```text
+----------------------------------------------------------------------+
|  DEVELOPMENT WORKSTATION (R&D sandbox)                    today:     |
|  - research, analytics and heavy simulations              simulator  |
|  - optional GPU acceleration, one GPU generation at a time           |
|  - validates strategies and exports lean JSON profiles               |
+-----------------------------------+----------------------------------+
                                    |  profiles copied over the LAN (SFTP / SCP)
                                    v
+----------------------------------------------------------------------+
|  HEADLESS EXECUTION NODE (small fanless PC, 8 GB RAM)     today:     |
|  - text-mode Linux, no desktop                            engine.py  |
|  - loads the profiles into memory at boot and runs them              |
|  - low-wear SQLite ledger, flushed once a day at midnight            |
|  - serial cable to a partner node for health and handover            |
|  - status API reachable only inside the local network                |
+----------------------------------------------------------------------+
```

- **The workstation** is where strategies are researched and tested. In this project, that is the simulator.
- **The node** only executes finished strategies. Keeping its job small lets it run on modest, silent, low-power hardware: the blueprint specifies an 8 GB "Kingdel" industrial mini PC. Minimal software and few disk writes keep it predictable and long-lived.

### Three cross-cutting principles

*Blueprint section 1, "Core Cross-Connections".*

1. **The inward friction screen.** Every buy or sell signal is checked against a model of real-world costs and is acted on only if it is still worth it after them. The blueprint's examples are a 0.50% Coinbase fee tier and a 31.3% short-term capital gains rate (California). The simulator implements this screen: it charges your live fee tier, slippage and tax, and it skips any entry whose profit target can't clear the `minimum_net_profit_gate`. See [the friction screen](Project_Documentation/model_configuration.md#5-the-friction-screen-worked-through).
2. **Outward value harvesting.** A share of trading profits, 10% in the blueprint, is regularly moved out of trading into low-volatility, capital-preservation holdings, such as short-term US Treasury bills or a Treasury bill ETF like SGOV. Profiles already have the `value_harvesting_rate` and `allocation_policy` fields, but harvesting is not modeled yet.
3. **A zero-trust network perimeter.** The node and its database live inside the private local network. Nothing faces the internet except the node's own outgoing exchange connections, and the status API is open only to the local subnet. The engine's dashboard exists today, but it listens on all network interfaces, so a firewall is what enforces this isolation (see [Security](Project_Documentation/trader.md#security)).

### Hardware and resource design

*Blueprint sections 2 and 3.*

- **Long-lived software.** The engine core is built on the Python standard library, plus the optional `pyserial` and `cupy` packages (neither is in `requirements.txt`, so the serial link and GPU helper do nothing until you install them), so that it keeps working for decades without dependency churn. In practice, the simulator uses numpy, matplotlib and the Coinbase SDK, and the engine needs the Coinbase SDK only for authentication.
- **The GPU mutual exclusion guard.** GPU simulations run on either a modern RTX card or a legacy GTX card, never both in one run, to protect the driver stack. The engine includes this guard (see the [GPU helper](Project_Documentation/trader.md#9-gpu-simulation-helper)). The simulator itself runs on the CPU.
- **The storage budget.** At 32 bytes per row, 10 years of data for five assets needs very little disk space:

  | Candle size | Rows | Size |
  |---|---|---|
  | Daily | 18,262 | 0.6 MB |
  | 15-minute | 1,753,200 | 56 MB |
  | 1-minute | 26,298,000 | 842 MB |

  Even the 1-minute data is under 1% of a 128 GB SSD. The simulator's own candle cache stores 64-bit values in SQLite instead, which takes more space: about 19 MB for 10 years of 15-minute BTC-USD candles.
- **The memory budget.** The node's whole runtime is planned at about 800 MB of its 8 GB:
  - Linux: 300 MB
  - Python: 50 MB
  - SQLite page cache and in-memory temporary tables: 200 MB each
  - the status server: 50 MB

  The engine sets a 50 MB SQLite cache (`cache_size=-50000`), but only on the set-up connection it opens and closes at start-up; later connections use SQLite's default.
- **Low disk wear.** The SQLite ledger uses write-ahead logging, and the design batches its writes into one daily flush at midnight. This protects the node's solid-state storage.

### Security and hardware cutover

*Blueprint sections 4 and 5.*

- **Firewall:** deny all incoming traffic by default, allow SSH, and allow the dashboard port only from the local subnet. The blueprint lists the exact `ufw` commands.
- **Zero-downtime hardware replacement:** a new node is brought in through these steps:
  1. Run it beside the old node.
  2. Mirror the `fortress.db` ledger to it with `scp`, plus a nightly sync.
  3. Hand over through the serial cable: the old node broadcasts its final state and stops routing orders, and the new node confirms and takes over, with no gap in the data.

### What exists today

| Blueprint element | Status | Where |
|---|---|---|
| Workstation simulation sandbox | Built: a desktop simulator with 10-year backtests, multi-model comparison and charts | `simulator.py`, `backtest.py`, `chart.py` |
| Declarative JSON profiles | Built and extended beyond the blueprint's fixed EMA fields: 24 indicators and rule formulas. Profiles in the blueprint's original format still load. | `model_config.py`, `indicators.py`, `rules.py` |
| Inward friction screen | Built in the simulator | `friction.py` |
| Outward value harvesting | Profile fields only | n/a |
| Headless node runtime | Foundation: settings, profile loading, Coinbase authentication check | `engine.py` |
| Live data streaming, live signal evaluation, order routing | Not built | n/a |
| Low-wear SQLite ledger | Database created with WAL; no tables or midnight flush yet | `engine.py` |
| LAN-only status API | Built; isolation depends on your firewall | `engine.py` |
| Serial interlock and cutover handshake | The serial port opens; no protocol yet | `engine.py` |
| GPU mutual exclusion guard | Built; not used by the simulator | `engine.py` |
| Shared system configuration | Designed but not built; `.env` holds these settings today | [system_config_structure.md](Project_Documentation/system_config_structure.md) |

The blueprint's code listing is an early snapshot of `engine.py`. The current engine adds `.env` settings and Coinbase authentication.

---

## Project layout

```text
Configurable_Crypto_Trading_Bot/
├── simulator.py              Simulator window (run this to backtest)
├── engine.py                 Trader runtime (status dashboard, Coinbase auth, ledger)
├── backtest.py               Candle download; the backtest engine
├── candle_store.py           Candle cache and stored indicators (SQLite)
├── performance_log.py        Model performance log (data/model_performance.csv)
├── model_search.py           Evolutionary model search (settings in search_config/)
├── model_config.py           Profile validation and compilation
├── indicators.py             Indicator library
├── rules.py                  Signal rule language
├── friction.py               Fees, slippage, tax and the friction screen
├── chart.py                  Charts for the simulator
├── requirements.txt          Python dependencies
├── .env.example              Template for your local .env
├── .vscode/                  Shared VS Code settings
├── config/
│   ├── .example/             Public template profiles (tracked)
│   └── *.json                Your private profiles (ignored by Git)
├── search_config/
│   ├── .example/             Public template search settings (tracked)
│   └── *.json                Your private search settings (ignored by Git)
├── data/                     Candle cache (candles.db) and model performance log, created on first run (ignored by Git)
├── ledger/                   Ledger database for the repository-root base directory
├── quant_system/             Default base directory of `python engine.py`
│   ├── config/               Profiles the engine loads (ignored by Git)
│   └── ledger/               The engine's ledger database
├── Project_Documentation/    Guides, references and design documents
└── tests/                    pytest test suite
```

Every folder has its own `README.md` that explains what the folder holds and how the tools use it:

| Folder | Its README covers |
|---|---|
| [Project_Documentation/](Project_Documentation/README.md) | An index of every documentation page, grouped into guides, references and design documents, with a suggested reading order |
| [config/](config/README.md) | Where private model profiles live, how to add one, and which tools read the folder |
| [config/.example/](config/.example/README.md) | What each template profile demonstrates, and the rules for public templates |
| [search_config/](search_config/README.md) | The model search's settings file: every key, writing atoms, and keeping the search space private |
| [ledger/](ledger/README.md) | Why the simulator creates this database, and what it holds |
| [quant_system/](quant_system/README.md) | The trader's default base directory: its profiles, its ledger, and path pitfalls |
| [quant_system/ledger/](quant_system/ledger/README.md) | The trader's ledger, and the blueprint's plans for it |
| [tests/](tests/README.md) | How to run the tests, what each file covers, and the shared test helpers |
| [.vscode/](.vscode/README.md) | The shared editor setting that pre-approves the test command |

The `data/` folder is created by the simulator's first run and is ignored by Git, so it has no README. Its contents are described in [Historical data and the cache](Project_Documentation/simulator.md#6-historical-data-and-the-cache).

---

## Configuration, secrets and private files

| Path | In Git | Contents |
|---|---|---|
| `.env` | Ignored | API keys and runtime settings; see [Setting up the .env file](Project_Documentation/trader.md#2-setting-up-the-env-file) |
| `.env.example` | Tracked | The template for `.env` |
| `config/*.json` | Ignored | Your private model profiles |
| `config/.example/` | Tracked | Public template profiles |
| `search_config/` | Ignored | Your model search settings, apart from the README and `.example/` |
| `quant_system/config/` | Ignored | The profiles loaded by `python engine.py` |
| `data/` | Ignored | The cached candle history and stored indicators (`candles.db`), and `model_performance.csv`. A `*_FIFTEEN_MINUTE.csv` cache left by an earlier version can be deleted once it has been imported. |
| `cdp_api_key*.json` | Ignored | Coinbase key files, if you keep them in the repository folder |
| `ledger/`, `quant_system/ledger/` | Tracked | A README and an empty `fortress.db` each (no tables yet). `.gitignore` does not exclude `*.db` files, so keep real trading records out of Git yourself. |

Keep proprietary strategies in `config/`, never in `config/.example/`.

---

## Running the tests

The test suite uses pytest, which is not in `requirements.txt`. Run it from the repository root:

```bash
pip install pytest
python -m pytest
```

Use `python -m pytest`, not plain `pytest`. Plain `pytest` can't import the project modules and fails at collection. The [tests README](tests/README.md) explains why, and what each test file covers.

The tests cover:

- indicators and rules
- profile validation
- the backtest: fills, stops, taxes, drawdown limits, flash crashes, start dates and the buy-and-hold benchmark
- the candle cache and its stored indicators
- the friction model and the charts
- the simulator's run workflow and the model performance log
- that every template in `config/.example/` is a valid profile
- the engine's settings and authentication

They use fake Coinbase clients, so they need no API key.

---

## Safety

- **The simulator only reads.** It lists your accounts, reads your fee tier and downloads candles from Coinbase, and it fetches the public Bitcoin network fee from mempool.space. If authentication fails with HTTP 401, it also asks api.ipify.org for your public IP address to show in the error. Its only changes to your machine are the files it creates (`data/`, `ledger/fortress.db`) and, if you accept its prompts, installing the Coinbase SDK and adding Python to your user `PATH`.
- **The trader cannot trade yet.** No code places orders. Use a **view-only** API key, and keep `COINBASE_ALLOW_TRADING`, `LIVE_EXECUTION_ACTIVE` and every profile's `live_execution_active` set to `false`. See [Trading safety switches](Project_Documentation/trader.md#10-trading-safety-switches).
- **The dashboard has no login.** Keep its port behind a firewall on your local network.
- **Backtests are optimistic.** Read [what the results do not include](Project_Documentation/simulator.md#what-the-results-do-not-include) before trusting a result.

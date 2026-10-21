# Model Configuration Guide

A **model profile** is a JSON file that fully describes one trading strategy:

- what it trades
- which candles it reads
- which indicators it computes
- when it buys and sells
- what trading costs it assumes
- how much capital it commits
- which safeguards protect it

To change a strategy, you edit its profile. You don't change any code.

This page explains how profiles work and documents every field. The indicator catalog and the full rule language are in a companion page, [indicator_reference.md](indicator_reference.md).

---

## Contents

1. [Where profiles live](#1-where-profiles-live)
2. [Create your first profile](#2-create-your-first-profile)
3. [Anatomy of a profile](#3-anatomy-of-a-profile)
4. [Field reference](#4-field-reference)
5. [The friction screen, worked through](#5-the-friction-screen-worked-through)
6. [Validation: what is checked and what is not](#6-validation-what-is-checked-and-what-is-not)
7. [Older profile format](#7-older-profile-format)
8. [Model configuration and system configuration](#8-model-configuration-and-system-configuration)

---

## 1. Where profiles live

| Location | Tracked by Git | Purpose |
|---|---|---|
| `config/.example/*.example.json` | Yes | Public templates. They contain no secrets or proprietary logic, so they are safe to share. |
| `config/*.json` | No | Your private working models. The simulator lists every `.json` file in this folder. |
| `<engine base directory>/config/*.json` | No | The profiles the trader engine loads when it starts. Running `python engine.py` reads `quant_system/config/`. See [trader.md](trader.md#6-model-profiles-in-the-engine). |

`.gitignore` excludes everything in `config/` except `.gitkeep` and the `.example/` folder, so a strategy saved there never reaches the repository. Keep proprietary models there.

The simulator names each model after its file name without `.json`. Any name works. When the simulator opens, it pre-selects `btc_macro_horizon.json` if that file exists.

Six templates ship with the project:

| Template | What it demonstrates |
|---|---|
| `btc_macro_horizon.example.json` | EMA trend following with a momentum screen and a trend-strength formula, on 15-minute candles |
| `btc_mean_reversion.example.json` | Bollinger Bands, RSI, ADX, historical volatility and a daily VWAP, on 1-hour candles |
| `btc_sma_30_100.example.json` | An SMA crossover with 20% positions, a [flash-crash reserve](#flash_crash_buy) and a [windowed drawdown stop](#the-two-drawdown-modes), on 15-minute candles |
| `btc_ema_10_100.example.json` | An all-in EMA crossover with a 50% drawdown limit, on 15-minute candles |
| `btc_growth_09.example.json` | A two-of-three vote that reads the ADX's `plus_di` and `minus_di` outputs, on daily candles |
| `btc_trend_vote_4in_1out.example.json` | A five-way trend vote that enters on 4 votes and exits on 1, on daily candles |

The templates demonstrate the syntax. They are not trading recommendations. As shipped, the friction screen blocks every entry the macro template signals (see [section 5](#5-the-friction-screen-worked-through)). The last four are backtested research models; [config/.example/README.md](../config/.example/README.md) describes each template and shows their results.

---

## 2. Create your first profile

1. **Copy a template** into `config/` under a new name:

   ```bash
   cp config/.example/btc_macro_horizon.example.json config/my_first_model.json
   ```

   In PowerShell: `Copy-Item config\.example\btc_macro_horizon.example.json config\my_first_model.json`

2. **Choose the market and candle size.** Set `product_id` (for example `ETH-USD`) and `period_granularity` (for example `ONE_HOUR`). Also set `ticker` (for example `ETH`), because the trader engine indexes profiles by it.
3. **Define indicators** under `indicators`, giving each one a name.
4. **Write the entry and exit rules** under `strategy.entry_signal` and `strategy.exit_signal`, using those names.
5. **Set costs, money and protection** in `friction_model`, `capital_allocation` and `safeguards`.
6. **Check the profile.** Start the simulator (`python simulator.py`), click **Refresh**, select your model and click **Load Profile**. Fix every problem it lists.
7. **Run it.** Click **Run Simulation**, then read the results (see [simulator.md](simulator.md#7-reading-the-results)).
8. **Iterate.** Change one thing at a time. Save variants as separate files and select them together to compare them side by side in one run.

### A minimal profile

Every field not listed here takes its default:

```json
{
  "ticker": "BTC",
  "product_id": "BTC-USD",
  "period_granularity": "ONE_HOUR",
  "indicators": {
    "fast": {"type": "EMA", "period": "12h"},
    "slow": {"type": "EMA", "period": "3d"}
  },
  "strategy": {
    "entry_signal": {"all": ["crosses_above(fast, slow)"]},
    "exit_signal": {"any": ["crosses_below(fast, slow)"]}
  }
}
```

With only these fields, the model behaves as follows:

- It starts with 10,000 in the quote currency (USD for `BTC-USD`).
- It puts all its cash into each trade.
- Each fill pays your live Coinbase taker fee, or 0.5% if the fee lookup fails.
- It assumes no slippage, no tax and no stops.

Add the other blocks to make the test realistic.

### Tips

- Prefer durations (`"12h"`, `"3d"`) to candle counts for periods. A duration keeps its meaning if you later change `period_granularity`.
- Use the `description` fields. The simulator ignores them, but they remind you, and anyone reading the profile, what each rule is for.
- Treat backtest results as an optimistic upper bound (see [what the numbers don't include](simulator.md#what-the-results-do-not-include)).

---

## 3. Anatomy of a profile

This is the macro trend template, `config/.example/btc_macro_horizon.example.json`:

```json
{
  "ticker": "BTC",
  "product_id": "BTC-USD",
  "strategy_horizon": "MACRO_TREND",
  "execution_platform": "COINBASE_ADVANCED",
  "period_granularity": "FIFTEEN_MINUTE",
  "indicators": {
    "fast_ema": {"type": "EMA", "period": "7.5h", "description": "Fast trend line (30 candles)."},
    "slow_ema": {"type": "EMA", "period": "45h", "description": "Slow macro trend filter (180 candles)."},
    "emergency_ema": {"type": "EMA", "period": "1h", "description": "Short momentum screen (4 candles)."}
  },
  "strategy": {
    "direction": "LONG_ONLY",
    "market_regime": "TREND_FOLLOWING",
    "entry_signal": {
      "description": "Fast EMA above slow EMA, emergency EMA confirming momentum, trend at least 1% strong.",
      "all": [
        "fast_ema > slow_ema",
        "emergency_ema > fast_ema",
        "(fast_ema - slow_ema) / slow_ema >= 0.01"
      ]
    },
    "exit_signal": {
      "description": "Exit when the fast EMA falls below the slow EMA, at the hard stop, or at the profit target.",
      "any": ["fast_ema < slow_ema"],
      "hard_stop_trigger": true,
      "profit_lock_target": 0.08
    }
  },
  "friction_model": {
    "exchange_fee_coverage_enabled": true,
    "commission_rate_source": "exchange_api",
    "fee_buffer_multiplier": 1.0,
    "fallback_commission_rate": 0.005,
    "tax_buffer_rate": 0.313,
    "slippage_buffer": 0.0025,
    "liquidity_fee_buffer": 0.5,
    "rebalancing_cost_adjustment": true
  },
  "capital_allocation": {
    "starting_capital": 10000,
    "target_position_fraction": 0.25,
    "value_harvesting_rate": 0.10,
    "allocation_policy": "convert_harvested_profit_to_configured_trading_currency"
  },
  "execution": {
    "order_style": "LIMIT_WITH_FRICTION_MARGIN",
    "live_execution_active": false,
    "max_orders_per_day": 12,
    "rebalance_interval_minutes": 15,
    "primary_exchange": "COINBASE_ADVANCED"
  },
  "safeguards": {
    "live_execution_active": false,
    "use_hard_stop": true,
    "hard_stop_percentage": 0.03,
    "minimum_net_profit_gate": 0.05,
    "max_drawdown_limit": 0.12,
    "volatility_cap": 0.08,
    "risk_budget_per_trade": 0.02
  }
}
```

Each block answers one question:

| Block | Question it answers | What the simulator uses today |
|---|---|---|
| Top-level fields | Which market and candle size? | `product_id`, `period_granularity` |
| `indicators` | What should be measured? | All of it |
| `strategy` | When to buy and when to sell? | All of it |
| `friction_model` | What does trading cost? | Fees, slippage, tax and the liquidity buffer |
| `capital_allocation` | How much money, and how much per trade? | `starting_capital`, `target_position_fraction` |
| `execution` | How are orders placed? | `max_orders_per_day` |
| `safeguards` | What limits the risk? | Hard stop, profit gate, drawdown limit and drawdown window |
| `tax_allocation` | When are tax reserves set aside? | Nothing yet |
| `regimes` (optional, experimental) | Which rules and risks fit the market's current situation? | All of it ([4.9](#49-regimes-experimental)) |

Every simulation report ends with a **Not modeled yet** line. It lists the same six settings for every model, whether or not the profile sets them: the fields marked **Not modeled yet** in the [field reference](#4-field-reference).

---

## 4. Field reference

In the **Status** column:

- **Simulated** means the simulator acts on the field.
- **Label** means the field is descriptive only.
- **Not modeled yet** means the field is planned but has no effect in the simulator yet. The report's **Not modeled yet** line lists these fields.
- **Not used yet** means no code reads the field today.
- **Engine** means only the trader engine uses the field.

Fractions are decimals: `0.08` means 8%.

### 4.1 Top-level fields

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `ticker` | Asset symbol, such as `"BTC"` | Required by the engine | Engine | The trader engine keys loaded profiles by this field. If two profiles in one engine share a ticker, the file that sorts later alphabetically replaces the earlier one. The simulator ignores it. |
| `product_id` | A Coinbase product, such as `"BTC-USD"` or `"ETH-USD"` | Required by the simulator | Simulated | The market whose candles are downloaded and traded. The part after the dash is the **quote currency**, in which the model's money is counted. A `BTC-` product also adds the Bitcoin network fee to the friction screen. |
| `period_granularity` | `ONE_MINUTE`, `FIVE_MINUTE`, `FIFTEEN_MINUTE`, `THIRTY_MINUTE`, `ONE_HOUR` (or `HOURLY`), `TWO_HOUR`, `SIX_HOUR`, `ONE_DAY` (or `DAILY`); case does not matter | `FIFTEEN_MINUTE` | Simulated | Candle size. Rules are evaluated once per candle, and periods given as candle counts are counted in this unit. Smaller candles mean much more data to download (see [simulator.md](simulator.md#6-historical-data-and-the-cache)). |
| `strategy_horizon` | Free text, such as `MACRO_TREND` or `SHORT_TERM` | none | Label | Describes the intended holding horizon. |
| `execution_platform` | Free text, such as `COINBASE_ADVANCED` | none | Label | Describes the intended venue. |

### 4.2 `indicators`

`indicators` maps names of your choosing to indicator definitions. Rules then use those names.

```json
"indicators": {
  "slow_ema": {"type": "EMA", "period": "45h"},
  "rsi":      {"type": "RSI", "period": 14},
  "bands":    {"type": "BOLLINGER", "period": "1d", "std_dev": 2},
  "macd":     {"type": "MACD", "fast": 12, "slow": 26, "signal": 9}
}
```

| Key | Meaning |
|---|---|
| `type` | The indicator type, in capitals. |
| Type parameters | Settings such as `period`, `std_dev` or `fast`. Parameters that have defaults can be left out. |
| `source` | The input series. Use a price field (`close`, `hlc3`, `ohlc4` and so on) or another indicator's output, for example `{"type": "SMA", "period": 5, "source": "rsi"}`. Defaults to `close`. |
| `plot` | `false` keeps the indicator off the chart. Its values still appear in the hover tooltip. |
| `label` | The display name in charts and reports. |
| `description` | Free text for readers. |

- **Periods** are a whole number of candles (`180`) or a duration (`"90m"`, `"45h"`, `"5d"`, `"2w"`).
- **Indicators with several outputs** are referred to as `name.output` in rules, for example `bands.upper` or `macd.signal`.
- **Unknown keys are rejected**, so a typo such as `"peroid"` is reported instead of being silently ignored.

**Available types** (24):

| Group | Types |
|---|---|
| Moving averages | `SMA`, `EMA`, `WMA`, `DEMA`, `TEMA`, `HMA`, `VWMA`, `VWAP` |
| Momentum | `RSI`, `MACD`, `STOCHASTIC`, `ROC`, `MOMENTUM`, `CCI`, `WILLIAMS_R`, `MFI` |
| Volatility | `ATR`, `BOLLINGER`, `KELTNER`, `DONCHIAN`, `STDDEV`, `VOLATILITY` |
| Trend strength | `ADX` |
| Volume | `OBV` |

For each type's parameters, outputs, formula, warm-up and typical use, see the [indicator catalog](indicator_reference.md#4-indicator-catalog).

### 4.3 `strategy`

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `direction` | `LONG_ONLY` | `LONG_ONLY` | Simulated | Only long-only strategies can be simulated. Any other value stops the run with an error. |
| `market_regime` | Free text, such as `TREND_FOLLOWING` or `MEAN_REVERSION` | none | Label | Describes the strategy type. |
| `entry_signal` | Object | Required | Simulated | When to buy. Needs at least one rule. |
| `exit_signal` | Object | `{}` | Simulated | When to sell. Rules are optional, and the block also holds the stop and target settings below. |
| `flash_crash_buy` | Object | Off | Simulated | Resting limit buys that catch sudden crashes. See [below](#flash_crash_buy). |

#### `entry_signal` and `exit_signal`

| Key | Meaning |
|---|---|
| `all` | A list of rules that must **all** hold. |
| `any` | A list of rules of which **at least one** must hold. |
| `description` | Free text for readers. |

When a block has both lists, every `all` rule and at least one `any` rule must hold. A single rule can be written as a plain string instead of a list. Rules are evaluated at each candle's close.

```json
"entry_signal": {"all": ["crosses_above(fast_ema, slow_ema)", "rsi < 70"]},
"exit_signal":  {"any": ["crosses_below(fast_ema, slow_ema)", "rsi > 80"]}
```

Rules can use:

- indicator names and price fields
- numbers and arithmetic
- comparisons, including chained ones such as `30 < rsi < 70`
- `and`, `or` and `not`
- functions such as `crosses_above`, `previous`, `highest` and `pct_change`

The full language, with many examples, is in [Writing signal rules](indicator_reference.md#5-writing-signal-rules). Rules are parsed against a whitelist and are never executed as Python, so a profile cannot run arbitrary code.

> **State or cross?** `fast_ema > slow_ema` is true on every candle of an uptrend. After a stop or profit lock closes a trade, such a rule can reopen a position at the same candle's close. If you want only one trade per signal, use `crosses_above(fast_ema, slow_ema)`, which is true only on the candle where the trend flips.

#### Exit settings inside `exit_signal`

| Field | Values | Default | Meaning |
|---|---|---|---|
| `hard_stop_trigger` | `true` or `false` | `true` | Whether the hard stop applies to this strategy. The stop also needs `safeguards.use_hard_stop: true` and a `safeguards.hard_stop_percentage`. |
| `profit_lock_target` | Fraction, such as `0.08` | `0` (off) | Sells when the price reaches this far above the entry fill price. It is also the expected gain the [friction screen](#5-the-friction-screen-worked-through) tests; without a target, the screen is off. |
| `profit_lock_trail` | Fraction from `0` up to (not including) `1`, such as `0.2` | `0` (off) | Needs `profit_lock_target`. Reaching the target then doesn't sell: it arms a trailing stop this far below the highest price since entry, from the next candle on, so the model keeps riding the run. Sells with the exit reason `TARGET_TRAIL`. On DOGE's sharp spikes this gave back more than it gained (a 15.96x model fell to 8-10x); test it before relying on it. |
| `trailing_stop` | Fraction from `0` up to (not including) `1`, such as `0.1` | `0` (off) | Sells when the price falls this far below the highest price since entry. The highest price is taken through the previous candle, because a candle's high and low can't be ordered within it. |

How the stops, the target and the exit rules interact within a candle is described in [How trades are simulated](simulator.md#8-how-trades-are-simulated).

#### `flash_crash_buy`

Flash-crash buys are resting limit orders placed far below the market to catch sudden crashes and erroneous prints. One famous example: Bitcoin briefly traded at $0.06 on Coinbase's GDAX exchange when it reopened after an outage on 15 April 2017.

```json
"flash_crash_buy": {
  "enabled": true,
  "reserve": 100,
  "levels": [
    {"drop": 0.3, "allocation": 0.25},
    {"drop": 0.6, "allocation": 0.25},
    {"drop": 0.9, "allocation": 0.25},
    {"drop": 0.99, "allocation": 0.25}
  ]
}
```

| Field | Values | Default | Meaning |
|---|---|---|---|
| `enabled` | `true` or `false` | `true` when the block is present | Turns the feature off without deleting the block. |
| `levels` | A list of `{"drop": ..., "allocation": ...}` | Required | Each `drop` (above 0 and below 1) places a buy that far below the previous candle's close. Once a crash has started, the levels stay priced from the close before it. Each `allocation` (above 0, at most 1) sizes that buy. |
| `reserve` | A positive amount, such as `100` | none | Sets aside a fixed amount of cash for these buys. The main strategy never trades it. Each level spends `allocation` of the reserve, so the allocations must add up to at most 1. Without a reserve, each level spends `allocation` of the cash available when it fills. |
| `description` | Free text | none | For readers. |

How a flash crash plays out:

- **The crash starts** when a candle trades down to the shallowest level. It lasts until the first candle that closes back above that level.
- **Fills:** each level fills at most once per crash, at its own price, and pays the **maker** fee because the order was resting on the book. Levels fill independently of the main strategy's position and don't count towards `max_orders_per_day`.
- **The sale:** what the levels bought is held until the crash recovers. It is then sold at the recovery candle's close, paying the taker fee and slippage.
- **The model never sells into a crash**, however long it lasts (April 2017's recovered within its 15-minute candle):
  - No stop or profit target fires on a crash candle, including the candle it recovers in, because that candle traded in the crash. Stops resume from the next candle.
  - No exit rule or drawdown limit sells until the recovery candle's close.
  - The crash is never removed from the data. Its real prices count in the indicators, the model's value and the drawdown figures.
  - If the crash never recovers, everything stays held.
- **Entries are not paused.** The entry rules still run during a crash, so the main strategy can buy at a crash candle's close. That position is then held through the crash like any other.

Without `flash_crash_buy`, there is no crash handling: a stop fills at whatever price a candle gapped to, as a stop-market order would.

A print like the April 2017 one lasts a fraction of a second, and only an order already resting at that price could catch it. Candle data can't show whether an order at that price would really have been filled, so **treat backtested flash-crash profits as an upper bound**. The Trades tab shows these fills as `FLASH_CRASH_BUY_<drop>` and `FLASH_REBOUND_SELL`, and the Metrics tab lists every crash and how it was handled.

### 4.4 `friction_model`

These settings model the real-world costs every trade pays.

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `exchange_fee_coverage_enabled` | `true` or `false` | `true` | Simulated | `false` removes exchange fees entirely. |
| `commission_rate_source` | `exchange_api`, `config` or `disabled` | `exchange_api` | Simulated | `exchange_api` looks up your account's live Coinbase fee tier at the start of each run and falls back to the profile rate if the lookup fails. `config` always uses the profile rate. `disabled` charges no fees. Any other value acts like `config`. |
| `fallback_commission_rate` | Fraction | `0.005` | Simulated | The **profile rate**: used when the source is `config` or the live lookup fails. `exchange_commission_rate` is an older name for the same setting and takes precedence if both are present. |
| `fee_buffer_multiplier` | Number, typically `1.0` to `1.5` | `1.0` | Simulated | Multiplies whichever fee rate is in use, as a safety cushion. A 0.5% rate with `1.25` becomes 0.625% per fill. |
| `slippage_buffer` | Fraction | `0` | Simulated | Price penalty on every market-style fill: buys fill this much higher and sells this much lower. |
| `tax_buffer_rate` | Fraction, such as `0.313` | `0` | Simulated | Tax reserved from each profitable completed trade and paid monthly (see [Taxes](#how-taxes-are-settled)). The friction screen also uses it. |
| `liquidity_fee_buffer` | Fraction | `0.5` | Simulated | A cushion used only by the friction screen: this fraction of the round-trip exchange fees plus the Bitcoin network fee for moving the position on-chain. It is never charged. |
| `liquidity_buffer` | — | — | Not modeled yet | Superseded by `liquidity_fee_buffer`. The simulator ignores it. |
| `rebalancing_cost_adjustment` | `true` or `false` | none | Not modeled yet | The simulator never resizes an open position. |

**Which fee applies:** entries, exits, stops and profit locks fill immediately, so they pay the **taker** rate. Flash-crash limit buys rest on the book, so they pay the **maker** rate. With live rates, maker and taker come from your fee tier. With the profile rate, both equal that rate.

The Metrics tab shows the rates a run actually used and where they came from: `Coinbase <your tier> tier (live)` when the lookup worked, or `profile fallback (<reason>)` when it failed.

#### How taxes are settled

The simulator reserves `tax_buffer_rate` of each profitable completed trade's net gain after fees. It moves that amount out of trading cash immediately, then pays the accumulated reserve at the first candle of the next UTC month. Losing trades do not offset tax reserved on profitable trades, either within the month or later.

- The **Tax reserve** panel under Model value shows the amount withheld and awaiting payment; it returns to zero at the monthly payment. The reserve is excluded from the model's trading equity, so clearing it at payment does not create a second drop in equity.
- The reserve is segregated from trading cash, so tax payments never sell holdings or force a strategy trade. Unrealized gains are not taxed.
- Any reserve for the unfinished final month remains withheld and is reported as tax held for the next monthly payment.

### 4.5 `capital_allocation`

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `starting_capital` | Positive number | `10000` | Simulated | Starting cash, in the quote currency. **Load Profile**, the Metrics tab and the Model value chart all show it. |
| `target_position_fraction` | Fraction from 0 to 1 | `1.0` | Simulated | The share of the model's cash committed to each entry, after setting aside any flash-crash reserve. The rest waits in cash and earns nothing. Values above 1 are not rejected and would simulate borrowing, so keep it at or below 1. |
| `value_harvesting_rate` | Fraction | none | Not modeled yet | Planned: the share of profits moved out of trading into capital preservation. This is the blueprint's "value harvesting" (see the [README](../README.md#the-quant-infrastructure-blueprint)). |
| `allocation_policy` | Text | none | Not used yet | Planned: how harvested profit is converted. |

### 4.6 `execution`

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `max_orders_per_day` | Whole number | Unlimited (`0` or absent) | Simulated | The maximum number of buys plus sells per UTC day. Once it is reached, new entries wait for the next UTC day. Exits are never blocked, and flash-crash fills don't count. |
| `order_style` | Text, such as `LIMIT_WITH_FRICTION_MARGIN` | none | Not used yet | The simulator fills at candle prices as described in [simulator.md](simulator.md#fills-fees-and-slippage). |
| `rebalance_interval_minutes` | Number | none | Not used yet | Rules are evaluated once per candle, whatever this says. |
| `primary_exchange` | Text | none | Label | |
| `live_execution_active` | `true` or `false` | `false` | Not used yet | No code places live orders today. Keep it `false`. The system-wide switch is `LIVE_EXECUTION_ACTIVE` in `.env`, which is not used yet either. |

### 4.7 `safeguards`

| Field | Values | Default | Status | Meaning |
|---|---|---|---|---|
| `use_hard_stop` | `true` or `false` | `false` | Simulated | Turns the hard stop on. It also needs `exit_signal.hard_stop_trigger`, which defaults to `true`. |
| `hard_stop_percentage` | Fraction, such as `0.03` | `0` | Simulated | How far below the **entry fill price** the hard stop sits. With `0.03`, a trade is sold if the price falls 3% below where it was bought. The stop is per trade, not a limit on the whole portfolio. |
| `minimum_net_profit_gate` | Fraction, such as `0.05` | Absent, which turns the screen off | Simulated | The minimum expected net return the [friction screen](#5-the-friction-screen-worked-through) requires before it allows an entry. |
| `max_drawdown_limit` | Fraction, such as `0.12` | `0` (off) | Simulated | The largest fall in the model's value it will tolerate. There are two modes, described below. |
| `drawdown_window` | Duration such as `"180d"` or `"26w"` (or a candle count) | Not set | Simulated | Makes `max_drawdown_limit` apply within a trailing window. The reports always show the worst drawdown within this window, or within 180 days when it is not set. |
| `volatility_cap` | Fraction | none | Not modeled yet | To stand aside in volatile markets today, add a `VOLATILITY` indicator rule to the entry signal, such as `volatility < 0.8`. |
| `risk_budget_per_trade` | Fraction | none | Not modeled yet | |
| `live_execution_active` | `true` or `false` | `false` | Not used yet | Keep it `false`. |

#### The two drawdown modes

- **Without `drawdown_window`:** the limit is checked at each candle's close, except during a flash crash, when it waits for the recovery candle's close. When the model's value falls `max_drawdown_limit` below its peak, any open position is sold at the close (`DRAWDOWN_LIMIT`) and trading pauses. Trading resumes only when the entry signal switches **off** and then fires again. The peak resets when trading resumes, so separate drawdowns can add up to more than the limit.
- **With `drawdown_window`** (such as `"180d"`): the limit applies to the model's value against its highest value within that trailing window, and it is enforced continuously.
  - During a trade, an equity stop (`DRAWDOWN_STOP`) sells at the exact price where the model's value would reach the limit below that high, after the sale's slippage and fee. Trading then pauses until a fresh entry signal.
  - New entries are taken only when there is room above the limit for two round trips' costs.
  - With `0.10` and `"180d"`, the model never loses more than 10% within any six months. There are two exceptions:
    - **A gap.** When the price gaps through the stop, the stop fills at the gapped price, as a real stop-market order would.
    - **A flash crash, with `flash_crash_buy` set.** The equity stop doesn't fire on crash candles, so the model's value can fall further while a crash lasts (see [flash_crash_buy](#flash_crash_buy)).

Pauses are shaded gray on the charts and listed in the Metrics tab.

### 4.8 `tax_allocation`

`tax_allocation` is an optional block for a scheduled tax reserve. The simulator does not use this schedule; it reserves tax per profitable trade using `friction_model.tax_buffer_rate` and pays the reserve monthly. The trader engine keeps the block with the loaded profile, so future code can read it through `get_model_configuration(ticker)`. Nothing checks its values yet. The four research templates (`btc_sma_30_100`, `btc_ema_10_100`, `btc_growth_09` and `btc_trend_vote_4in_1out`) each include a monthly example.

| Field | Meaning |
|---|---|
| `enabled` | Whether tax allocation is active. |
| `rate` | The share of realized profits sent to the tax reserve. |
| `schedule.interval` | `WEEKLY`, `MONTHLY`, `QUARTERLY` or `YEARLY`. |
| `schedule.day_of_month` | The day used by a monthly or yearly schedule. |
| `schedule.month` | The month used by a yearly schedule, such as `12` for December. |
| `schedule.time_utc` | The trigger time, in UTC. |
| `description` | A human-readable description of the policy. |

### 4.9 `regimes` (experimental)

`regimes` lets one profile change its rules and risk settings with the market's situation. Each regime has a `when` rule that detects the situation, and settings that apply while it is active. The rest of the profile is the **base** regime, used whenever no `when` matches. The simulator and stress tests support regimes; the trader engine doesn't trade on them yet.

```json
"regimes": {
  "confirm": "7d",
  "min_hold": "30d",
  "on_switch": "keep",
  "list": [
    {"name": "trend", "when": "close > lock * 1.25",
     "exit_signal": {"any": ["votes <= 0"]}, "safeguards": {"use_hard_stop": false}},
    {"name": "chop", "when": "adx.adx < 20",
     "entry_signal": {"all": ["rsi < 30"]}, "exit_signal": {"any": ["rsi > 55"]},
     "safeguards": {"use_hard_stop": true, "hard_stop_percentage": 0.08},
     "capital_allocation": {"target_position_fraction": 0.5}}
  ]
}
```

| Field | Values | Default | Meaning |
|---|---|---|---|
| `list` | Regimes, checked in order | Required | The first regime whose `when` holds is the **candidate** on each candle. |
| `confirm` | Duration or candle count | `1` | The **detection lag**: a candidate must stay the same for this long before it becomes the active regime. |
| `min_hold` | Duration or candle count | `1` | Once a regime is active, it stays active at least this long, so the model doesn't flip back and forth. |
| `on_switch` | `keep` or `close` | `keep` | What happens to an open position when the regime changes. `keep` holds it under the new regime's exits; `close` sells it at that candle's close (exit reason `REGIME_SWITCH`). |

Each regime can set:

- `name` (required to be unique) and `when` (required), plus an optional `description`.
- `entry_signal` and `exit_signal`, written like the strategy's. A regime that leaves either out uses the base strategy's. Inside `exit_signal`, `trailing_stop`, `profit_lock_target`, `profit_lock_trail` and `hard_stop_trigger` apply to that regime.
- `safeguards.use_hard_stop` and `safeguards.hard_stop_percentage`.
- `capital_allocation.target_position_fraction`.

Settings a regime leaves out come from the base profile. Indicators, friction, flash-crash buys, the drawdown limit and `max_orders_per_day` are shared by every regime. The friction screen uses the base profile's `profit_lock_target`.

Detection only uses candles up to the current close, so it lags. Stops, trailing stops and flash-crash buys still act inside every candle, so give the fast protection to each regime's stops, and keep `confirm` for the slower question of which regime the market is in. Each result reports the regime switches and the share of candles spent in each regime under `regimes`, and its `series.regime` gives the active regime on every candle. **Load Profile** lists each regime's detector, rules and stops; the simulator's charts mark where the regimes switched and which one was in use (see [Regime marks](simulator.md#regime-marks)).

---

## 5. The friction screen, worked through

Before taking any entry signal, the simulator asks one question: *if this trade reached its profit target, would it still make at least `minimum_net_profit_gate` after every cost and tax?* If not, it skips the signal. The screen keeps a model from taking trades whose target is too small to be worth the friction.

The screen is on only when the profile sets **both** `exit_signal.profit_lock_target` (non-zero) and `safeguards.minimum_net_profit_gate`.

```text
at_target = (1 - fee)^2 x (1 + profit_lock_target) x (1 - slippage) / (1 + slippage) - 1
cushion   = liquidity_fee_buffer x (2 x fee + network_fee / position_value)
pre_tax   = at_target - cushion
expected  = pre_tax x (1 - tax_buffer_rate)     (tax applies only to a positive pre_tax)

enter only if expected >= minimum_net_profit_gate
```

In these formulas:

- `fee` is the per-fill taker rate after `fee_buffer_multiplier`.
- `network_fee` applies only to `BTC-` products. It is the live "half-hour" fee rate from mempool.space times a typical 141-vbyte transaction, valued at the candle's price. If the lookup fails, it counts as 0.
- `position_value` is the cash the entry would commit: the model's cash, less any flash-crash reserve, times `target_position_fraction`.
- The price used is the signal candle's close.

**Worked example: the macro template at its 0.5% profile rate**

Inputs: 0.5% fee, 0.25% slippage, 8% target, 50% liquidity buffer, 31.3% tax, network fee ignored.

| Step | Calculation | Result |
|---|---|---|
| Round trip at the target | (1 - 0.005)² × 1.08 × 0.9975 / 1.0025 - 1 | +6.39% |
| Liquidity cushion | 0.5 × (2 × 0.5%) | -0.50% |
| Before tax | 6.39% - 0.50% | +5.89% |
| After 31.3% tax | 5.89% × 0.687 | **+4.05%** |

The result, +4.05%, is below the 5% gate, so **every entry is blocked**. A live fee tier gives a different figure, but any fee of 0.1% or more per fill still blocks every entry. At 0.9% per fill, for example, the result is about +3.18%. A run of this template reports the blocked signals as "Entry signals blocked by the screen" in Metrics and "Blocked signals" in Comparison. Each signal is counted once, even if it stays true for many candles.

To let trades through, do one of the following:

- Raise the target to about 9.5%.
- Lower the gate to 4%.
- Reduce the assumed costs, if your real costs are lower.

The Metrics tab prints this calculation on every run, so you can see whether the screen passes. It uses the first test candle's close and a first trade of `starting_capital` × `target_position_fraction`.

---

## 6. Validation: what is checked and what is not

**Load Profile** runs these checks, and **Run Simulation** repeats them before it starts:

- JSON syntax. Errors are reported with a line and column, such as `Expecting ',' delimiter: line 12 column 5`.
- Indicator names, types, parameters and periods, and indicator sources, including loops between indicators.
- Every rule's syntax, names and functions. Typos come with suggestions, such as `did you mean fast_ema?`.
- That `entry_signal` has at least one rule.
- That `period_granularity` is supported.
- That `starting_capital` is a positive number.
- The `flash_crash_buy` levels and reserve.

These are checked when a model's simulation starts, and an error stops the whole run:

- `strategy.direction` must be `LONG_ONLY`.
- `exit_signal.trailing_stop` must be at least 0 and below 1.
- `safeguards.drawdown_window`, when set, must be a candle count or a duration such as `"180d"`.
- The start date must not be after the last candle.

**Not checked:** fields outside `indicators` are read with defaults, so a misspelled key is **silently ignored**. For example, a block named `"safegaurds"` means no safeguards, and `"profit_lock_targt"` means no target and no friction screen. Out-of-range values, such as a negative fee, are also accepted. After changing costs or safeguards, check the **Friction model** section of the Metrics tab to confirm the settings took effect.

For a table of error messages and their fixes, see [Validation and troubleshooting](indicator_reference.md#8-validation-and-troubleshooting).

---

## 7. Older profile format

Profiles written before configurable indicators used fixed fields: `fast_window_periods`, `slow_window_periods`, `emergency_ema_periods`, `price_source`, `fast_ema_gt_slow_ema` and so on. Such profiles still load. The simulator translates them into the current form, and **Load Profile** says that it did. For the translation and the equivalent current form, see [Migrating older profiles](indicator_reference.md#9-migrating-older-profiles).

---

## 8. Model configuration and system configuration

A model profile describes **one strategy**. Settings that apply to the whole system belong elsewhere:

- exchange credentials
- the dashboard port
- the global trading switches

Today those settings live in the local `.env` file, which [trader.md](trader.md#2-setting-up-the-env-file) documents. A shared JSON system configuration file is planned; its design is in [system_config_structure.md](system_config_structure.md).

A model cannot grant itself trading access. Trading permissions and switches are system-level settings by design.

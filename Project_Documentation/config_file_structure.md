# Configuration File Structure and Meaning

This project keeps trading logic in local JSON configuration files so the strategy can be tuned without changing the code. The configuration is intentionally separated from the production engine, and private runtime models remain local to the machine.

## Purpose

Configuration files define:
- which asset is being traded
- the strategy horizon and market regime
- technical indicator windows and thresholds
- execution and friction assumptions
- safety and capital preservation rules

These files are read by the engine and simulator at runtime. They are meant to be editable, portable, and safe to version as templates when no secrets or proprietary logic are included.

## File locations

- Public template examples live in the repository under `config/.example/`
- Local private runtime models are intended to live in the ignored `config/` directory or another local-only path
- The production engine reads configuration profiles using the JSON format shown below

## Example model: BTC macro trend profile

The main BTC strategy profile is stored in `config/btc_macro_horizon.json`.

```json
{
  "ticker": "BTC",
  "product_id": "BTC-USD",
  "strategy_horizon": "MACRO_TREND",
  "execution_platform": "COINBASE_ADVANCED",
  "period_granularity": "FIFTEEN_MINUTE",
  "indicators": {
    "fast_ema": {"type": "EMA", "period": "7.5h", "source": "close"},
    "slow_ema": {"type": "EMA", "period": "45h", "source": "close"},
    "emergency_ema": {"type": "EMA", "period": "1h", "source": "close"}
  },
  "strategy": {
    "direction": "LONG_ONLY",
    "market_regime": "TREND_FOLLOWING",
    "entry_signal": {
      "description": "Bullish macro trend when the fast EMA remains above the slow EMA, the emergency EMA confirms momentum, and the trend is at least 1% strong.",
      "all": [
        "fast_ema > slow_ema",
        "emergency_ema > fast_ema",
        "(fast_ema - slow_ema) / slow_ema >= 0.01"
      ]
    },
    "exit_signal": {
      "description": "Exit when trend structure weakens, the hard stop is reached, or the profit target is locked in.",
      "any": ["fast_ema < slow_ema"],
      "hard_stop_trigger": true,
      "profit_lock_target": 0.08
    }
  },
  "friction_model": {
    "exchange_fee_coverage_enabled": true,
    "commission_rate_source": "exchange_api",
    "fee_buffer_multiplier": 1.25,
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

## Field-by-field meaning

### Top-level fields

- `ticker`: The asset symbol, such as `BTC`
- `product_id`: The exchange instrument string, such as `BTC-USD`
- `strategy_horizon`: The time horizon for the model. Valid examples include `MACRO_TREND`, `MEDIUM_TERM`, or `SHORT_TERM`, depending on the chosen strategy design.
- `execution_platform`: The exchange or platform the model is meant to operate on, such as `COINBASE_ADVANCED` or `KRAKEN_SPOT`.
- `period_granularity`: Candle size. Valid values are `ONE_MINUTE`, `FIVE_MINUTE`, `FIFTEEN_MINUTE`, `THIRTY_MINUTE`, `ONE_HOUR` (or `HOURLY`), `TWO_HOUR`, `SIX_HOUR` and `ONE_DAY` (or `DAILY`). Indicator periods given in candles are counted in this unit.

> If a field supports multiple valid input types or values, those options are documented here so the user understands the intended choices and their practical differences.

### `strategy`

This block defines the actual directional philosophy of the model.

- `direction`: Whether the model is long-only, short-only, or hedged. Valid options can include `LONG_ONLY`, `SHORT_ONLY`, or `LONG_SHORT`.
- `market_regime`: The model design type. Common examples include `TREND_FOLLOWING`, `MEAN_REVERSION`, or `VOLATILITY_BREAKOUT`.
- `entry_signal`: when to enter a trade, written as rule formulas over the profile's indicators.
  - `all`: a list of rules that must all hold, such as `"fast_ema > slow_ema"`.
  - `any`: a list of rules of which at least one must hold.
  - `description`: free text for readers.
- `exit_signal`: when to leave a trade. It takes the same `all`/`any` rule lists (optional; they may be left out to exit only through stops and targets), plus:
  - `hard_stop_trigger`: whether the `safeguards.hard_stop_percentage` stop applies. Accepts `true` or `false`.
  - `profit_lock_target`: sell once the price is this fraction above the entry, such as `0.08` for +8%. It is also the expected gain the friction screen tests.
  - `trailing_stop` (optional): sell when the price falls this fraction below the highest price since entry, such as `0.1` for 10%. The high is taken through the previous candle, since a candle's high and low can't be ordered within it. Shown as `TRAILING_STOP` in the Trades tab.
- `flash_crash_buy` (optional): resting limit buys that catch sudden crashes and erroneous prints, such as the $0.06 BTC trade when Coinbase reopened after an outage on 15 April 2017. Every candle, each level places a buy `drop` below the previous candle's close. With `reserve`, a fixed amount such as `100` is set aside for these buys: each level uses `allocation` of the reserve (the allocations must add up to at most 1), and the main strategy never trades that cash. Without `reserve`, each level uses `allocation` of the cash available at that moment. If a candle trades down to a level, it fills at the level's price, paying the **maker** fee since the order was resting on the book, and sells at that candle's close, paying the taker fee and slippage. Levels fill shallowest first, independently of the main strategy's position, and don't count towards `max_orders_per_day`.

  A candle that trades down to the shallowest level starts a **flash crash**, which lasts until the first candle that closes back above that level. The model never sells into one; the reserve buys it instead, and holds what it bought until the crash recovers, selling at that recovery candle's close:
  - **Recovers within an hour** (like April 2017's, which recovered in 15 minutes): treated as a bad data point. For the strategy, the crash candles' prices below the level are replaced by the pre-crash close, so indicators, stops, the model's value and drawdown figures all ignore it.
  - **Lasts longer than an hour:** the model keeps holding. No stop, profit target or exit signal sells during the crash, and the real prices count in its value and drawdown. Normal trading resumes from the candle it recovers in; if it never recovers, everything stays held.

  The Metrics tab lists each flash crash, how deep it went, how long it lasted and how it was handled. Without `flash_crash_buy`, there is no crash handling: stops fill at whatever price a candle gapped to, as a stop-market order would.

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

  A print like the April 2017 one lasts a fraction of a second, so only an order already resting at that price can catch it; that is what these levels model. Candle data can't show whether an order at a given price would actually have been filled, so treat backtested flash-crash profits as an upper bound. The Trades tab lists each fill as `FLASH_CRASH_BUY_<drop>`, and the Metrics tab summarizes them.

Rules can compare indicators and prices, do arithmetic, and use functions such as `crosses_above(a, b)`, `previous(x, n)` and `highest(x, n)`. The full language, with examples, is in [indicator_reference.md](indicator_reference.md#5-writing-signal-rules).

### `indicators`

This section names the technical indicators the strategy's rules use. Each entry maps a name to a definition with a `type` and that type's parameters:

```json
"indicators": {
  "slow_ema": {"type": "EMA", "period": "45h"},
  "rsi":      {"type": "RSI", "period": 14},
  "bands":    {"type": "BOLLINGER", "period": "1d", "std_dev": 2},
  "macd":     {"type": "MACD", "fast": 12, "slow": 26, "signal": 9}
}
```

- Periods are a whole number of candles (`180`) or a duration (`"45h"`, `"5d"`), so a strategy keeps its meaning across candle sizes.
- `source` picks the input series: a price field (`close`, `hlc3`, `ohlc4` and so on) or another indicator's name.
- Indicators with several outputs are used in rules as `name.output`, for example `bands.upper` or `macd.signal`.

Available types: moving averages (`SMA`, `EMA`, `WMA`, `DEMA`, `TEMA`, `HMA`, `VWMA`, `VWAP`), momentum (`RSI`, `MACD`, `STOCHASTIC`, `ROC`, `MOMENTUM`, `CCI`, `WILLIAMS_R`, `MFI`), volatility (`ATR`, `BOLLINGER`, `KELTNER`, `DONCHIAN`, `STDDEV`, `VOLATILITY`), trend strength (`ADX`) and volume (`OBV`). Every type's parameters, outputs, formula and typical use are documented in [indicator_reference.md](indicator_reference.md), along with strategy recipes and troubleshooting.

The example above follows the blueprint's macro trend design: a faster moving average leads the slower trend filter, and a short emergency screen confirms momentum. Profiles written with the older fixed fields (`fast_window_periods`, `slow_window_periods`, `emergency_ema_periods`, `price_source`) still load and are translated automatically; see [migrating older profiles](indicator_reference.md#9-migrating-older-profiles).

### `friction_model`

This section models execution costs and real-world drag in a dynamic way.

- `exchange_fee_coverage_enabled`: whether the model includes exchange fee coverage in trade friction calculations. Accepts `true` or `false`.
- `commission_rate_source`: source of the fee. Supported values include `exchange_api` (the default: the account's live Coinbase fee tier, looked up at the start of each simulation), `config`, or `disabled`. The simulator fills immediately, so both the buy and the sell pay the taker rate.
- `fee_buffer_multiplier`: safety multiplier applied to the fetched exchange fee rate. Typical values are `1.0` to `1.5`, where `1.0` means no extra cushion and `1.25` adds a safety margin.
- `fallback_commission_rate`: fee rate used if the live exchange fee lookup fails. This is a numeric decimal value like `0.005`. Older profiles may use `exchange_commission_rate` for the same purpose.
- `tax_buffer_rate`: tax on realized gains, based on local legal assumptions, such as `0.313`. The simulator nets each calendar year's realized gains and losses, pays this rate on a net gain when the next year begins, and carries net losses forward to offset later gains. It is also applied in the friction screen.
- `slippage_buffer`: expected price drift when filling orders. This is a decimal value such as `0.0025`.
- `liquidity_fee_buffer`: safety margin used by the friction screen, as a fraction of the round-trip exchange fees plus the Bitcoin network transaction fee (looked up live) for moving the position on-chain. Defaults to `0.5` (a 50% cushion). It is never charged at the moment of a trade.
- `liquidity_buffer`: superseded by `liquidity_fee_buffer` and ignored by the simulator.
- `rebalancing_cost_adjustment`: whether the model includes costs from rebalancing. Accepts `true` or `false`. Not modeled yet, since the simulator never resizes an open position.

The simulator's **friction screen** checks every entry signal: if the trade reached `strategy.exit_signal.profit_lock_target`, would its return after round-trip fees, slippage, the liquidity buffer and tax still be at least `safeguards.minimum_net_profit_gate`? If not, the signal is skipped. The screen is off when either setting is missing.

This design removes the need for a hard-coded exchange commission rate in the model while still preserving a safe fallback. If the exchange reports a 0.50% fee and the buffer multiplier is 1.25, the effective modeled fee becomes 0.625%.

> Note: exchange commission refresh scheduling is not defined inside the model config. That belongs in a shared system config, because it is a platform-wide operational setting that applies across all models, not a strategy-specific input.

### `capital_allocation`

This controls the model's starting value, position sizing and profit harvesting.

- `starting_capital`: the model's starting value, in the product's quote currency (USD for `BTC-USD`), such as `10000`. The simulator starts every run with this much cash; **Load Profile**, the Metrics tab and the Model value chart all show it. It must be a positive number and defaults to `10000` when left out.
- `target_position_fraction`: share of the model's current value committed to each trade, such as `0.25`
- `value_harvesting_rate`: percentage of gains routed into the configured trading currency
- `allocation_policy`: logic for converting harvested profits into the configured trading currency

This matches the blueprint requirement that profits are harvested into a local capital-preservation flow, but without forcing a specific reserve instrument. The harvested value is converted into the trading currency configured for the automated system.

### `tax_allocation`

This block is specifically for configurable tax reserve allocations and scheduling.

- `enabled`: whether tax allocation is active
- `rate`: percentage of realized profits sent to the tax reserve
- `schedule.interval`: cadence such as `MONTHLY`, `YEARLY`, `QUARTERLY`, or `WEEKLY`
- `schedule.day_of_month`: day-of-month used for a monthly or yearly schedule
- `schedule.month`: month number used for a yearly schedule, such as `12` for December
- `schedule.time_utc`: time the allocation is triggered in UTC
- `description`: human-readable description of fiscal policy

This allows the model to allocate taxes and reserves on a schedule without hard-coding a specific vehicle. The harvested value is converted into the same currency used by the configured automated trader.

### `execution`

This controls how the strategy interacts with the exchange.

- `order_style`: order type and execution logic. Common values include `LIMIT_WITH_FRICTION_MARGIN`, `MARKET`, or `STOP_LIMIT`.
- `live_execution_active`: whether the model is currently allowed to route live trades. Accepts `true` or `false`.
- `max_orders_per_day`: guardrail for activity volume. This is usually a number such as `12`.
- `rebalance_interval_minutes`: how often the model revisits the portfolio position, such as `15` or `60`.
- `primary_exchange`: target venue for execution. Examples include `COINBASE_ADVANCED`, `KRAKEN_SPOT`, or a custom internal routing label.

### `safeguards`

This is the risk firewall layer.

- `live_execution_active`: prevents accidental live trading when false. Accepts `true` or `false`.
- `use_hard_stop`: enables emergency shutdown logic. Accepts `true` or `false`.
- `hard_stop_percentage`: max allowed portfolio drawdown before stopping, such as `0.03` for a 3% stop.
- `minimum_net_profit_gate`: minimum net gain threshold before allowing active execution, such as `0.05` for 5%.
- `max_drawdown_limit`: maximum tolerated drawdown, such as `0.10` for 10%. How it is enforced depends on `drawdown_window`:
  - **Without `drawdown_window`:** when the model's value falls this far below its peak, any position is closed and trading pauses until the entry signal switches off and fires again. The peak resets when trading resumes, so separate drawdowns can add up.
  - **With `drawdown_window`**, such as `"180d"`: the limit applies to the model's value against its highest value within that trailing window, and is enforced continuously. During a trade, an equity stop sells at the exact price where the model's value, after the sale's slippage and fee, would reach the limit below that high (`DRAWDOWN_STOP` in the Trades tab); trading then pauses until a fresh entry signal. New entries are only taken with room above the limit for two round trips' costs. With `0.10` and `"180d"`, the model never loses more than 10% within any six months, except when price gaps through the stop: a stop fills at the gapped price, as a real stop-market order would.
- `drawdown_window` (optional): the trailing period for `max_drawdown_limit`, as a duration such as `"180d"` or `"26w"`. Every run reports the worst drawdown within this window (or within 180 days when it is not set) in the Metrics and Comparison tabs. Tax payments count as withdrawals rather than losses in all drawdown figures.
- `volatility_cap`: volatility threshold beyond which the strategy stays cautious, often a decimal like `0.08`.
- `risk_budget_per_trade`: per-trade risk exposure limit, usually a small decimal such as `0.02`.

These fields are essential for keeping the model within the blueprint’s safety-first approach. If a user wants a more aggressive setup, the values can be adjusted; if a more cautious setup is desired, the thresholds can be tightened.

## System configuration vs model configuration

The project separates model-local settings from shared system settings.

- Model config files define strategy-specific behavior: asset, indicators, execution thresholds, safety gates, and model assumptions.
- System config files define cross-model platform settings such as exchange fee refresh cadence, API connection behavior, and shared operational policies.

This split avoids overloading a single model with settings that apply globally across every model. For example, the weekly exchange commission refresh schedule belongs in a system config file, not the BTC model config.

## Template vs private model

The repository should keep only example or blank configuration templates in version control. Actual trading models should remain local and private to avoid exposing proprietary strategy logic.

For example:
- `config/.example/btc_macro_horizon.example.json` is a safe public template
- a local private version such as `config/btc_macro_horizon.json` may exist on the machine but should stay out of Git tracking

## Best practice

When creating new models:
1. copy an existing template
2. update the asset and signal logic
3. adjust friction assumptions for the target exchange
4. set realistic risk and drawdown limits
5. keep the file local if it contains proprietary strategy logic

## Summary

The config file is the model’s operating contract: it tells the engine what to trade, how to trade it, what costs to include, and how to protect capital. In this project, the config file acts as the complete declarative strategy profile for both the production engine and the simulation GUI.

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
  "strategy": {
    "direction": "LONG_ONLY",
    "market_regime": "TREND_FOLLOWING",
    "entry_signal": {
      "description": "Bullish macro trend when the fast EMA remains above the slow EMA and the emergency EMA confirms momentum.",
      "fast_ema_gt_slow_ema": true,
      "emergency_ema_gt_fast_ema": true,
      "minimum_trend_strength": 0.01
    },
    "exit_signal": {
      "description": "Exit when trend structure weakens or the hard stop is reached.",
      "fast_ema_lt_slow_ema": true,
      "hard_stop_trigger": true,
      "profit_lock_target": 0.08
    }
  },
  "indicators": {
    "fast_window_periods": 30,
    "slow_window_periods": 180,
    "emergency_ema_periods": 4,
    "price_source": "close",
    "trend_filter": "EMA_CROSSOVER"
  },
  "friction_model": {
    "exchange_fee_coverage_enabled": true,
    "commission_rate_source": "exchange_api",
    "fee_buffer_multiplier": 1.25,
    "fallback_commission_rate": 0.005,
    "tax_buffer_rate": 0.313,
    "slippage_buffer": 0.0025,
    "liquidity_buffer": 0.01,
    "rebalancing_cost_adjustment": true
  },
  "capital_allocation": {
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
- `period_granularity`: Data frequency being used. Valid examples include `FIFTEEN_MINUTE`, `ONE_MINUTE`, `HOURLY`, or `DAILY`.

> If a field supports multiple valid input types or values, those options are documented here so the user understands the intended choices and their practical differences.

### `strategy`

This block defines the actual directional philosophy of the model.

- `direction`: Whether the model is long-only, short-only, or hedged. Valid options can include `LONG_ONLY`, `SHORT_ONLY`, or `LONG_SHORT`.
- `market_regime`: The model design type. Common examples include `TREND_FOLLOWING`, `MEAN_REVERSION`, or `VOLATILITY_BREAKOUT`.
- `entry_signal`: Rules that define when to enter a trade.
- `exit_signal`: Rules that define when to exit or lock profits.

Example concepts:
- `fast_ema_gt_slow_ema`: fast moving average above slow moving average
- `emergency_ema_gt_fast_ema`: confirms momentum is staying positive
- `minimum_trend_strength`: minimum threshold needed before entering

> When a field has multiple valid patterns, the meaning of each option is explained in the documentation rather than assumed from the sample.

### `indicators`

This section defines the technical inputs used by the strategy.

- `fast_window_periods`: shorter EMA lookback, typically a small value such as `10` or `30`
- `slow_window_periods`: longer EMA lookback, such as `50`, `100`, or `180`
- `emergency_ema_periods`: short-term confirmation window, such as `4` or `9`
- `price_source`: the input series for trend evaluation. Valid options generally include `close`, `hlc3`, or `ohlc4`.
- `trend_filter`: indicator style used to validate trend strength. Examples include `EMA_CROSSOVER`, `PRICE_POSITION`, or `VOLATILITY_FILTER`.

This follows the blueprint’s macro trend design, where a faster moving average leads the slower trend filter and adds a short emergency screen for quick confirmation.

### `friction_model`

This section models execution costs and real-world drag in a dynamic way.

- `exchange_fee_coverage_enabled`: whether the model includes exchange fee coverage in trade friction calculations. Accepts `true` or `false`.
- `commission_rate_source`: source of the fee. Supported values include `exchange_api`, `config`, or `disabled`.
- `fee_buffer_multiplier`: safety multiplier applied to the fetched exchange fee rate. Typical values are `1.0` to `1.5`, where `1.0` means no extra cushion and `1.25` adds a safety margin.
- `fallback_commission_rate`: fee rate used if the live exchange fee lookup fails. This is a numeric decimal value like `0.005`.
- `tax_buffer_rate`: tax consideration for realized gains, based on local legal assumptions. This is usually a decimal such as `0.313`.
- `slippage_buffer`: expected price drift when filling orders. This is a decimal value such as `0.0025`.
- `liquidity_buffer`: cost from lower trading liquidity situations. Typical values are small decimals such as `0.01`.
- `rebalancing_cost_adjustment`: whether the model includes costs from rebalancing. Accepts `true` or `false`.

This design removes the need for a hard-coded exchange commission rate in the model while still preserving a safe fallback. If the exchange reports a 0.50% fee and the buffer multiplier is 1.25, the effective modeled fee becomes 0.625%.

> Note: exchange commission refresh scheduling is not defined inside the model config. That belongs in a shared system config, because it is a platform-wide operational setting that applies across all models, not a strategy-specific input.

### `capital_allocation`

This controls position sizing and profit harvesting.

- `target_position_fraction`: maximum share of capital assigned to the strategy
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
- `max_drawdown_limit`: maximum tolerated drawdown before risk reduction, such as `0.12`.
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

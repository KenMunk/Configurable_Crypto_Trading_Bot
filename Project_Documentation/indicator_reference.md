# Indicators and Signal Rules Reference

Model profiles describe a trading strategy in two parts:

1. **Indicators**: named calculations over the price history, such as a 50-period moving average, a 14-period RSI or 20-period Bollinger Bands.
2. **Signal rules**: formulas over those indicators that decide when to enter and exit, such as `fast_ema > slow_ema and rsi < 70`.

This document covers every indicator the simulator supports, how to configure it, and how to write rules with it. The implementation lives in `indicators.py` (the library), `rules.py` (the formula language) and `model_config.py` (validation and legacy translation).

---

## Contents

1. [Quick start](#1-quick-start)
2. [Defining indicators](#2-defining-indicators)
3. [Price fields](#3-price-fields)
4. [Indicator catalog](#4-indicator-catalog)
5. [Writing signal rules](#5-writing-signal-rules)
6. [How signals become trades](#6-how-signals-become-trades)
7. [Strategy recipes](#7-strategy-recipes)
8. [Validation and troubleshooting](#8-validation-and-troubleshooting)
9. [Migrating older profiles](#9-migrating-older-profiles)
10. [Charts and tooltips](#10-charts-and-tooltips)

---

## 1. Quick start

A complete trend-following model needs only two blocks:

```json
"indicators": {
  "fast_ema": {"type": "EMA", "period": "12h"},
  "slow_ema": {"type": "EMA", "period": "3d"},
  "rsi":      {"type": "RSI", "period": 14}
},
"strategy": {
  "direction": "LONG_ONLY",
  "entry_signal": {
    "description": "Uptrend that is not yet overbought.",
    "all": ["crosses_above(fast_ema, slow_ema)", "rsi < 70"]
  },
  "exit_signal": {
    "description": "Trend reversal, or the stop or target.",
    "any": ["crosses_below(fast_ema, slow_ema)"],
    "hard_stop_trigger": true,
    "profit_lock_target": 0.08
  }
}
```

Click **Load Profile** in the simulator to check it. The Metrics tab either confirms the profile is valid and lists its indicators and rules, or lists every problem it found.

Working examples live in `config/.example/`:

| File | What it demonstrates |
|---|---|
| `btc_macro_horizon.example.json` | EMA trend following with a momentum screen and a trend-strength formula |
| `btc_mean_reversion.example.json` | Bollinger Bands, RSI, ADX, historical volatility and a daily VWAP together |

The examples illustrate the syntax. They are not trading recommendations: at Coinbase's entry fee tier, neither is profitable in the simulator.

---

## 2. Defining indicators

`indicators` is an object that maps **names** to **definitions**:

```json
"indicators": {
  "slow_ema": {"type": "EMA", "period": 180, "source": "close", "description": "Macro trend filter."}
}
```

### Names

- Letters, digits and underscores only, and not starting with a digit: `fast_ema`, `rsi_14`, `bands`.
- Names are case-sensitive, and rules refer to indicators by these names.
- Reserved names cannot be used: the price fields (`open`, `high`, `low`, `close`, `volume`, `hl2`, `hlc3`, `ohlc4`), the rule function names (`previous`, `highest` and so on), and `True`/`False`.

### Definition keys

| Key | Required | Meaning |
|---|---|---|
| `type` | yes | One of the indicator types in [section 4](#4-indicator-catalog), in capitals: `"EMA"`, `"BOLLINGER"` and so on. |
| *parameters* | depends | Type-specific settings such as `period`, `std_dev` or `fast`. Parameters with defaults can be left out. |
| `source` | no | The series the indicator is computed from (default `"close"`). This can be a price field or **another indicator's name**, for example `{"type": "SMA", "period": 5, "source": "rsi"}` smooths an RSI. Only types that list `source` accept it. |
| `plot` | no | `false` leaves the indicator off the charts; its values still appear in the hover tooltip. Every indicator is drawn by default: price-scale indicators on the price chart, oscillators in their own panel (see [section 10](#10-charts-and-tooltips)). |
| `label` | no | Display name for the chart legend and reports. Defaults to `name (TYPE period)`. |
| `description` | no | Free text for readers of the profile; ignored by the simulator. |

Unknown keys are rejected, so a typo like `"peroid"` is caught instead of being silently ignored.

### Periods: candles or durations

Every period-like parameter (`period`, `fast`, `slow`, `signal`, `k_period`, `d_period`, `smooth`, `atr_period`) accepts either:

- **A whole number of candles**: `30` means 30 candles of the profile's `period_granularity`.
- **A duration string**: a number followed by `m` (minutes), `h` (hours), `d` (days) or `w` (weeks), such as `"90m"`, `"7.5h"`, `"5d"` or `"2w"`. It is converted to the nearest whole number of candles.

Durations keep a strategy's meaning when you change candle size. With `FIFTEEN_MINUTE` candles, `"45h"` is 180 candles; with `ONE_HOUR` candles it becomes 45 candles, still spanning 45 hours. A duration shorter than one candle is an error.

| Granularity | `"1h"` | `"1d"` | `"1w"` |
|---|---|---|---|
| `ONE_MINUTE` | 60 | 1,440 | 10,080 |
| `FIFTEEN_MINUTE` | 4 | 96 | 672 |
| `ONE_HOUR` | 1 | 24 | 168 |
| `ONE_DAY` | error | 1 | 7 |

### Warm-up

An indicator has no value until it has seen enough history: a 180-period moving average needs 180 candles. Before then its value is *unknown*, shown as "warming up" in the chart tooltip. Rules treat unknown values as "not yet true", so **no trade is entered or exited on a half-formed indicator**. Long periods therefore delay the first possible trade by that much history.

Approximate warm-up lengths:

| Type | Candles before the first value |
|---|---|
| SMA, WMA, EMA, VWMA, ROC, MOMENTUM, STDDEV, DONCHIAN, WILLIAMS_R, CCI, ATR | `period` |
| DEMA / TEMA | about 2x / 3x `period` |
| HMA | `period + sqrt(period)` |
| RSI, MFI | `period + 1` |
| ADX | about `2 x period` |
| MACD `line` / `signal` | `slow` / `slow + signal` |
| STOCHASTIC `k` / `d` | `k_period (+ smooth)` / plus `d_period` |
| BOLLINGER, KELTNER | `period` (KELTNER: the longer of `period` and `atr_period`) |
| VOLATILITY | `period + 1` |
| OBV, anchored VWAP | none |

---

## 3. Price fields

These series are always available to rules and as indicator sources:

| Name | Value |
|---|---|
| `open` | Candle open price |
| `high` | Candle high price |
| `low` | Candle low price |
| `close` | Candle close price |
| `volume` | Candle volume, in the base currency (BTC for BTC-USD) |
| `hl2` | `(high + low) / 2`, the median price |
| `hlc3` | `(high + low + close) / 3`, the typical price |
| `ohlc4` | `(open + high + low + close) / 4` |

---

## 4. Indicator catalog

Each entry lists the parameters (with defaults; **required** means there is no default), the outputs, the formula, and how it is commonly read. Indicators with several outputs are referred to as `name.output` in rules, for example `bands.upper` or `macd.signal`.

"Price scale" indicators are measured in price units and drawn over the price chart. "Oscillators" have their own scale, so each one is drawn in its own panel under the price chart.

### 4.1 Moving averages (price scale)

Moving averages smooth price to reveal trend direction. Shorter periods react faster but whipsaw more; longer periods are steadier but lag.

#### SMA: Simple moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** the arithmetic mean of the last `period` values.
- **Use:** the classic trend filter. Price above a rising 200-period SMA is widely read as an uptrend.

```json
"trend_filter": {"type": "SMA", "period": "8d"}
```

#### EMA: Exponential moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `EMA = EMA_prev + a x (value - EMA_prev)` with `a = 2 / (period + 1)`, seeded with the first value.
- **Use:** weights recent prices more heavily than the SMA, so it turns sooner. It is the basis of most crossover systems.

#### WMA: Linearly weighted moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `sum(weight_i x value_i) / sum(weight_i)`, with weights `period, period-1, ..., 1` from newest to oldest.
- **Use:** a middle ground between the SMA and the EMA in responsiveness.

#### DEMA: Double exponential moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `2 x EMA(x) - EMA(EMA(x))`
- **Use:** removes much of the EMA's lag. It is good for faster trend signals, but noisier.

#### TEMA: Triple exponential moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `3 x E1 - 3 x E2 + E3`, where `E1 = EMA(x)`, `E2 = EMA(E1)` and `E3 = EMA(E2)`
- **Use:** even less lag than DEMA.

#### HMA: Hull moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `WMA(2 x WMA(x, period/2) - WMA(x, period), sqrt(period))`
- **Use:** very smooth and very responsive. A popular alternative to EMA crossovers is to trade the HMA's direction: `hma > previous(hma)`.

#### VWMA: Volume-weighted moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `sum(value x volume) / sum(volume)` over the window
- **Use:** gives heavy-volume candles more say. When the VWMA sits above the SMA of the same period, volume is backing the up-moves.

#### VWAP: Volume-weighted average price
- **Parameters:** either `"anchor": "day"` or a rolling `period`
- **Output:** one value
- **Formula:** `sum(typical price x volume) / sum(volume)`, where the typical price is `hlc3`. With `"anchor": "day"` the sums restart at 00:00 UTC each day, as institutional desks use it. With `period` they cover a rolling window, which suits 24/7 crypto markets.
- **Use:** the benchmark of "fair" price for the session. Buying below VWAP is considered good execution, and price reclaiming VWAP is a common intraday trigger.

```json
"session_vwap": {"type": "VWAP", "anchor": "day"},
"rolling_vwap": {"type": "VWAP", "period": "1d"}
```

### 4.2 Momentum oscillators

#### RSI: Relative strength index
- **Parameters:** `period` (14), `source` (`close`)
- **Output:** one value from 0 to 100
- **Formula:** Wilder's RSI, `100 - 100 / (1 + average gain / average loss)`, with gains and losses smoothed by Wilder's method (`a = 1 / period`, seeded with a simple average).
- **Use:** above 70 is conventionally overbought and below 30 oversold. In strong trends RSI can stay extreme for a long time, so pair it with a trend filter. A cross back above 30 (`crosses_above(rsi, 30)`) is a common entry trigger.

#### MACD: Moving average convergence/divergence
- **Parameters:** `fast` (12), `slow` (26), `signal` (9), `source` (`close`). `fast` must be shorter than `slow`.
- **Outputs:** `line` = `EMA(fast) - EMA(slow)`; `signal` = `EMA(line, signal)`; `histogram` = `line - signal`
- **Use:** `crosses_above(macd.line, macd.signal)` is the textbook buy signal. `macd.line > 0` confirms the fast average is above the slow one. A shrinking histogram warns that momentum is fading.

```json
"macd": {"type": "MACD", "fast": 12, "slow": 26, "signal": 9}
```

#### STOCHASTIC: Stochastic oscillator
- **Parameters:** `k_period` (14), `d_period` (3), `smooth` (1)
- **Outputs:** `k` = `100 x (close - lowest low) / (highest high - lowest low)` over `k_period`, optionally smoothed by an SMA of `smooth`; `d` = SMA of `k` over `d_period`
- **Use:** where the close sits within its recent range. Above 80 is overbought and below 20 oversold. `crosses_above(stoch.k, stoch.d)` below 20 is a classic buy. `"smooth": 3` gives the "slow stochastic".

#### ROC: Rate of change
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value, a percentage
- **Formula:** `100 x (value / value n candles ago - 1)`
- **Use:** raw momentum. `roc > 0` means price is higher than `period` candles ago. Useful for momentum ranking and filters.

#### MOMENTUM: Price momentum
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value, in price units
- **Formula:** `value - value n candles ago`
- **Use:** like ROC but in absolute terms.

#### CCI: Commodity channel index
- **Parameters:** `period` (20)
- **Output:** one value, unbounded, usually within about ±200
- **Formula:** `(typical price - SMA(typical price)) / (0.015 x mean absolute deviation)`
- **Use:** readings above +100 signal unusual strength and below -100 unusual weakness. It is used both for breakouts and for mean reversion.

#### WILLIAMS_R: Williams %R
- **Parameters:** `period` (14)
- **Output:** one value from -100 to 0
- **Formula:** `-100 x (highest high - close) / (highest high - lowest low)`
- **Use:** an inverted stochastic. Above -20 is overbought and below -80 oversold.

#### MFI: Money flow index
- **Parameters:** `period` (14)
- **Output:** one value from 0 to 100
- **Formula:** an RSI-like ratio of positive to negative money flow, where money flow is typical price times volume, counted positive when the typical price rose.
- **Use:** "volume-weighted RSI". Above 80 is overbought and below 20 oversold. Divergence from price can warn of reversals.

### 4.3 Volatility

#### ATR: Average true range
- **Parameters:** `period` (14)
- **Output:** one value, in price units
- **Formula:** Wilder-smoothed true range, where true range = the largest of `high - low`, `|high - previous close|` and `|low - previous close|`.
- **Use:** the standard measure of how far price typically moves per candle. Use it to size stops and filters: `close > previous(close) + 2 * atr` flags an unusually large up-move. Divide by price to compare across time: `atr / close < 0.01`.

#### BOLLINGER: Bollinger Bands (price scale)
- **Parameters:** `period` (20), `std_dev` (2), `source` (`close`)
- **Outputs:** `middle` = SMA; `upper` / `lower` = middle ± `std_dev` x population standard deviation; `bandwidth` = `(upper - lower) / middle`; `percent_b` = `(value - lower) / (upper - lower)`
- **Use:** price outside the bands is statistically stretched. Mean-reversion systems buy `close < bands.lower`, and breakout systems buy `close > bands.upper`. A very low `bandwidth` (a "squeeze") often precedes a big move. A `percent_b` of 0 means price is at the lower band and 1 at the upper band. Only `upper`, `middle` and `lower` are drawn on the chart.

```json
"bands": {"type": "BOLLINGER", "period": "1d", "std_dev": 2}
```

#### KELTNER: Keltner Channels (price scale)
- **Parameters:** `period` (20), `atr_period` (10), `multiplier` (2)
- **Outputs:** `middle` = EMA of the typical price over `period`; `upper` / `lower` = middle ± `multiplier` x ATR(`atr_period`)
- **Use:** an ATR-based envelope that is smoother than Bollinger Bands. Closing outside the channel signals a strong trend. Bollinger Bands inside Keltner Channels is the classic "TTM squeeze" setup (see the recipes).

#### DONCHIAN: Donchian Channels (price scale)
- **Parameters:** `period` (20)
- **Outputs:** `upper` = highest high, `lower` = lowest low, `middle` = their average, over the last `period` candles **including the current one**
- **Use:** the Turtle Traders' breakout system. Because the current candle is included, test a breakout against the *previous* channel: `close > previous(breakout.upper)`. The condition `close > breakout.upper` can never be true, since the upper band includes the current high.

#### STDDEV: Standard deviation
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value, in price units
- **Formula:** population standard deviation over the window.
- **Use:** raw dispersion. Compare it to price for a relative measure: `stddev / close`.

#### VOLATILITY: Historical volatility
- **Parameters:** `period` (required), `annualize` (`true`), `source` (`close`)
- **Output:** one value, a fraction: `0.60` means 60% annualized volatility
- **Formula:** standard deviation of log returns over the window. With `annualize`, it is multiplied by `sqrt(candles per year)`, using 365 days a year since crypto trades daily.
- **Use:** the measure risk desks quote. Use it to stand aside in disorderly markets (`volatility < 0.8`) or to require movement (`volatility > 0.3`). BTC's long-run annualized volatility is roughly 50-80%.

### 4.4 Trend strength

#### ADX: Average directional index
- **Parameters:** `period` (14)
- **Outputs:** `adx` (trend strength, 0-100), `plus_di` (upward pressure), `minus_di` (downward pressure)
- **Formula:** Wilder's directional movement system. `+DI` and `-DI` are smoothed directional moves divided by ATR, and ADX is the smoothed `|+DI - -DI| / (+DI + -DI)`.
- **Use:** ADX measures *how strong* a trend is, not its direction. Above 25 is a trending market (favor trend systems) and below 20 a ranging market (favor mean reversion). `trend.plus_di > trend.minus_di` gives the direction.

```json
"trend": {"type": "ADX", "period": 14}
```

### 4.5 Volume

#### OBV: On-balance volume
- **Parameters:** none
- **Output:** one value, cumulative volume
- **Formula:** a running total that adds the candle's volume on up-closes and subtracts it on down-closes.
- **Use:** its level is arbitrary; its direction matters. Rising OBV confirms an uptrend: `obv > previous(obv, '1d')`. OBV rising while price falls (divergence) can precede a reversal.

VWMA, VWAP and MFI (above) also use volume.

---

## 5. Writing signal rules

### 5.1 Where rules go

Rules live in the strategy's `entry_signal` and `exit_signal` blocks, in one or both of two lists:

| Key | Meaning |
|---|---|
| `all` | Every rule in the list must hold (logical AND). |
| `any` | At least one rule in the list must hold (logical OR). |

If a block has both, **every `all` rule and at least one `any` rule** must hold.

`entry_signal` needs at least one rule. `exit_signal` may have none, in which case positions close only through the hard stop, the profit lock or the drawdown limit. A single rule may be written as a string instead of a list: `"any": "fast_ema < slow_ema"`.

### 5.2 Syntax

A rule is a formula that is true or false at each candle. You can use:

| Element | Examples |
|---|---|
| Names | `close`, `rsi`, `bands.upper`, `macd.signal` |
| Numbers | `70`, `0.01`, `-1.5` |
| Arithmetic | `+`, `-`, `*`, `/`, parentheses |
| Comparisons | `>`, `>=`, `<`, `<=`, `==`, `!=` |
| Chained comparisons | `30 < rsi < 70` (both must hold) |
| Logic | `and`, `or`, `not` |
| Functions | see below |

Operator precedence follows the usual rules: arithmetic, then comparisons, then `not`, then `and`, then `or`. Use parentheses when in doubt: `(fast_ema - slow_ema) / slow_ema >= 0.01`.

Rules can use nothing else. There are no variables, strings (except duration periods in functions), lists or attribute access beyond `indicator.output`. Rules are parsed against a whitelist and never run as Python code, so a profile cannot execute arbitrary commands.

### 5.3 Functions

| Function | Meaning |
|---|---|
| `crosses_above(a, b)` | `a` moved from at or below `b` on the previous candle to above `b` on this one |
| `crosses_below(a, b)` | `a` moved from at or above `b` to below `b` on this candle |
| `previous(x, n)` | `x` as it was `n` candles ago (`n` defaults to 1) |
| `change(x, n)` | `x - previous(x, n)` (`n` defaults to 1) |
| `pct_change(x, n)` | `x / previous(x, n) - 1`, for example `0.02` for +2% (`n` defaults to 1) |
| `highest(x, n)` | the highest value of `x` over the last `n` candles, including this one |
| `lowest(x, n)` | the lowest value of `x` over the last `n` candles, including this one |
| `abs(x)` | absolute value |
| `min(a, b)` / `max(a, b)` | the smaller or larger of two values at each candle |

The period argument `n` must be a constant: a candle count (`3`) or a quoted duration (`'1d'`). `a`, `b` and `x` can be any expression: `crosses_above(close, bands.lower)`, `previous(fast_ema - slow_ema)`.

**Crosses versus states:** `fast_ema > slow_ema` is true on every candle of an uptrend, while `crosses_above(fast_ema, slow_ema)` is true only on the candle where the trend flips. Use crosses for "act on the change", and states for "stay in while the condition holds".

### 5.4 Unknown values

While an indicator is warming up, its value is unknown. Comparisons with an unknown value are unknown, and an unknown rule never fires. With `and`/`or`, an unknown part counts as false: `rsi < 30 or close > bands.upper` can still fire on the band condition while RSI warms up. `not` of an unknown is still unknown.

Division by zero also gives an unknown value rather than an error.

### 5.5 Examples

| Idea | Rule |
|---|---|
| Uptrend | `fast_ema > slow_ema` |
| Fresh golden cross | `crosses_above(sma_50, sma_200)` |
| Trend at least 1% strong | `(fast_ema - slow_ema) / slow_ema >= 0.01` |
| Oversold bounce | `crosses_above(rsi, 30)` |
| RSI in a neutral zone | `40 <= rsi <= 60` |
| Close outside the lower band | `close < bands.lower` |
| Bollinger squeeze | `bands.bandwidth < 0.02` |
| MACD bullish cross above zero | `crosses_above(macd.line, macd.signal) and macd.line > 0` |
| Strong uptrend by ADX | `trend.adx > 25 and trend.plus_di > trend.minus_di` |
| 20-candle breakout | `close > previous(breakout.upper)` |
| New 1-day high | `close >= highest(high, '1d')` |
| Up more than 3% in 4 hours | `pct_change(close, '4h') > 0.03` |
| Volume spike | `volume > 3 * volume_avg` (with `"volume_avg": {"type": "SMA", "period": "1d", "source": "volume"}`) |
| Rising trend line | `slow_ema > previous(slow_ema, '1h')` |
| Calm enough to trade | `atr / close < 0.01` or `volatility < 0.8` |
| Price above session VWAP | `close > session_vwap` |

---

## 6. How signals become trades

Each candle is processed in this order:

1. **Taxes** for a finished calendar year are settled when a new year begins. They count as withdrawals, not losses, in every drawdown figure.
2. **Flash crashes**, if `strategy.flash_crash_buy` is set: any resting limit buy the candle traded down to fills at its price and is held until the crash recovers. A crash that recovers within an hour is ignored by everything below; during a longer one, steps 3 and 4 are skipped until it recovers (see `config_file_structure.md`).
3. **Exits**, if a position is open, checked in priority order:
   1. **Stops**: the hard stop (if `safeguards.use_hard_stop` and `exit_signal.hard_stop_trigger` are true, at `entry price x (1 - hard_stop_percentage)`) and, with `safeguards.drawdown_window`, the drawdown stop (at the price where the model's value would fall `max_drawdown_limit` below its high in the window). If the candle's low reaches either, the higher one fills, at its price or at the open if price gapped below it.
   2. **Profit lock**: if the candle's high reaches `entry price x (1 + profit_lock_target)`, it sells at the target, or at the open if price gapped above it.
   3. **Exit rules**: if the exit rules hold at the candle's close, it sells at the close. The Trades tab shows the reason `EXIT_SIGNAL`.
4. **Drawdown limit** without a window: if equity has fallen `max_drawdown_limit` from its peak, any position closes and trading pauses. After either kind of drawdown exit, trading resumes only once the entry signal has switched **off** and then fires again.
5. **Entries**, if no position is open: the entry rules must hold at the close, `max_orders_per_day` must allow it, and the signal must pass the **friction screen** (see `config_file_structure.md`). The screen uses `exit_signal.profit_lock_target` as the expected gain, so a model without a profit target is not screened. Entries fill at the close, with the reason `ENTRY_SIGNAL`.

Rules see only the current and past candles, so there is no look-ahead: a rule evaluated at a candle's close uses that close and earlier data.

---

## 7. Strategy recipes

Each recipe shows the `indicators` and signal blocks; combine them with the rest of a profile. Tune the numbers with the simulator, and remember the friction screen: the profit target must clear round-trip fees, the liquidity buffer and tax.

### 7.1 Moving average crossover (trend following)

```json
"indicators": {
  "fast": {"type": "EMA", "period": "12h"},
  "slow": {"type": "EMA", "period": "3d"}
},
"entry_signal": {"all": ["crosses_above(fast, slow)"]},
"exit_signal":  {"any": ["crosses_below(fast, slow)"], "hard_stop_trigger": true, "profit_lock_target": 0.12}
```

### 7.2 Golden cross with a volume filter

```json
"indicators": {
  "sma_50":     {"type": "SMA", "period": "50d"},
  "sma_200":    {"type": "SMA", "period": "200d"},
  "volume_avg": {"type": "SMA", "period": "20d", "source": "volume"}
},
"entry_signal": {"all": ["crosses_above(sma_50, sma_200)", "volume > volume_avg"]},
"exit_signal":  {"any": ["crosses_below(sma_50, sma_200)"]}
```

On 15-minute candles, a 200-day SMA needs 19,200 candles of warm-up (200 days), so the first possible trade comes 200 days into the data.

### 7.3 MACD with an ADX trend filter

```json
"indicators": {
  "macd":  {"type": "MACD", "fast": 12, "slow": 26, "signal": 9},
  "trend": {"type": "ADX", "period": 14}
},
"entry_signal": {"all": ["crosses_above(macd.line, macd.signal)", "macd.line > 0", "trend.adx > 25"]},
"exit_signal":  {"any": ["crosses_below(macd.line, macd.signal)"]}
```

### 7.4 RSI and Bollinger mean reversion

```json
"indicators": {
  "bands": {"type": "BOLLINGER", "period": "1d", "std_dev": 2},
  "rsi":   {"type": "RSI", "period": 14},
  "trend": {"type": "ADX", "period": 14}
},
"entry_signal": {"all": ["close < bands.lower", "rsi < 30", "trend.adx < 20"]},
"exit_signal":  {"any": ["close > bands.middle", "crosses_above(rsi, 60)"], "hard_stop_trigger": true}
```

### 7.5 Donchian breakout (Turtle style)

```json
"indicators": {
  "entry_channel": {"type": "DONCHIAN", "period": "20d"},
  "exit_channel":  {"type": "DONCHIAN", "period": "10d"}
},
"entry_signal": {"all": ["close > previous(entry_channel.upper)"]},
"exit_signal":  {"any": ["close < previous(exit_channel.lower)"]}
```

### 7.6 Volatility squeeze breakout

Bollinger Bands contract inside Keltner Channels during quiet periods; the breakout from that squeeze is the trade.

```json
"indicators": {
  "bands":   {"type": "BOLLINGER", "period": 20, "std_dev": 2},
  "channel": {"type": "KELTNER", "period": 20, "atr_period": 20, "multiplier": 1.5},
  "mom":     {"type": "MOMENTUM", "period": 12}
},
"entry_signal": {"all": [
  "previous(bands.upper) < previous(channel.upper)",
  "bands.upper > channel.upper",
  "mom > 0"
]},
"exit_signal": {"any": ["mom < 0"]}
```

### 7.7 VWAP pullback in an uptrend

```json
"indicators": {
  "session_vwap": {"type": "VWAP", "anchor": "day"},
  "trend":        {"type": "EMA", "period": "2d"}
},
"entry_signal": {"all": ["close > trend", "crosses_above(close, session_vwap)"]},
"exit_signal":  {"any": ["close < trend"]}
```

### 7.8 Adding a risk filter to any strategy

Append a volatility or ATR condition to the entry `all` list to stand aside in disorderly markets:

```json
"volatility": {"type": "VOLATILITY", "period": "7d"}
```
```json
"all": ["...your entry rules...", "volatility < 0.9"]
```

---

## 8. Validation and troubleshooting

The simulator checks a profile when you click **Load Profile** and before each run. It reports every problem at once:

| Message | Cause and fix |
|---|---|
| `Indicator 'x' has unknown type 'BOLINGER'; choose one of: ...` | Misspelled `type`. Types are upper case; see section 4. |
| `Indicator 'x' (EMA) needs "period"` | A required parameter is missing. |
| `Indicator 'x' (ADX) does not take smoothing; its parameters are: period` | An unknown or misspelled key. |
| `'x' period '3 fortnights' is not a candle count or a duration` | Use a whole number, or forms like `'90m'`, `'12h'`, `'5d'`, `'2w'`. |
| `... is shorter than one candle` | For example `'5m'` on 15-minute candles. Use a longer duration. |
| `Indicator name 'close' is reserved` | Rename the indicator. |
| `Rule '...': unknown name 'fsat_ema'; did you mean fast_ema?` | A typo in a rule, or an indicator that isn't defined. |
| `'macd' has several outputs; refer to one of: macd.line, ...` | Multi-output indicators need `.output` in rules. |
| `Rule '...' is not a valid formula` | A syntax error, such as an unbalanced parenthesis or a dangling operator. |
| `Rule '...': '...' is not allowed` | Something outside the rule language, such as a list, index or lambda. |
| `the period in previous() must be a number of candles or a duration` | Function periods must be constants, not indicators. |
| `strategy.entry_signal needs at least one rule in "all" or "any"` | An empty entry block. |
| `Indicator sources form a loop: a -> b -> a` | Two indicators use each other as `source`. |
| `MACD needs fast shorter than slow` | Swap `fast` and `slow`. |

**No trades at all?** Check, in order:
1. The Metrics tab's **Entry signals blocked by the screen** count. If it is high, the profit target cannot clear friction and tax at the `minimum_net_profit_gate`; raise the target or lower the gate.
2. Warm-up. Very long periods leave little data to trade.
3. Contradictory rules, such as `rsi < 30` together with `close > bands.upper`.
4. Crosses combined with states in `all`. A cross is true on only one candle, so every other `all` rule must be true on that same candle.

---

## 9. Migrating older profiles

Profiles written before configurable indicators used fixed fields:

```json
"indicators": {"fast_window_periods": 30, "slow_window_periods": 180, "emergency_ema_periods": 4, "price_source": "close"},
"strategy": {
  "entry_signal": {"fast_ema_gt_slow_ema": true, "emergency_ema_gt_fast_ema": true, "minimum_trend_strength": 0.01},
  "exit_signal":  {"fast_ema_lt_slow_ema": true}
}
```

They still load: the simulator translates them automatically, and **Load Profile** notes that it did. The equivalent current form is:

```json
"indicators": {
  "fast_ema":      {"type": "EMA", "period": 30,  "source": "close"},
  "slow_ema":      {"type": "EMA", "period": 180, "source": "close"},
  "emergency_ema": {"type": "EMA", "period": 4,   "source": "close"}
},
"strategy": {
  "entry_signal": {"all": ["fast_ema > slow_ema", "emergency_ema > fast_ema", "(fast_ema - slow_ema) / slow_ema >= 0.01"]},
  "exit_signal":  {"any": ["fast_ema < slow_ema"]}
}
```

The old `trend_filter` field is no longer used.

---

## 10. Charts and tooltips

Every indicator in the profile gets its own line on the **Chart** tab.

- **Price-scale indicators** (moving averages, VWAP, and the upper, middle and lower lines of Bollinger, Keltner and Donchian) are drawn over the close price. Each indicator has its own color, assigned in the order the indicators are defined; band edges are thinner lines in their indicator's color. There is no limit on how many are drawn: past the fifth, the colors repeat with a dashed line, then a dotted one, so every indicator keeps a distinct color-and-style pair in the legend.
- **Oscillators** (RSI, MACD, Stochastic, ROC, Momentum, CCI, Williams %R, MFI, ATR, standard deviation, volatility, ADX and OBV) each get a **panel of their own** under the price chart, because their scales differ from price and from each other. Multi-output oscillators draw every output as a separate line in their panel, with a small legend: MACD's `line`, `signal` and `histogram`, ADX's `adx`, `plus_di` and `minus_di`, and Stochastic's `k` and `d`.
- Panels show their conventional reference levels as thin lines: RSI 30/70, MFI and Stochastic 20/80, Williams %R -80/-20, CCI ±100, ADX 20 and 25, and zero for MACD, ROC and Momentum. RSI, MFI, Stochastic and Williams %R keep their fixed scale (0-100, or -100-0); other panels scale to their data.
- All panels share the price chart's time axis: zooming or panning any panel with the toolbar moves them all, and double-clicking a trade in the Trades tab zooms every panel to it.
- Hovering anywhere shows a crosshair through every panel and a tooltip, in the top corner away from the cursor, listing **every** indicator's value at that candle, including each output of multi-output indicators.
- Set `"plot": false` on an indicator to leave it off the chart (and remove its panel); its values still appear in the tooltip.
- Buy (▲) and sell (▼) markers show where rules, stops and targets filled.
- Each panel takes a slice of the chart's height, so profiles with many oscillators give each panel less room; enlarge the window or hide the less important ones with `"plot": false`.

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
| `btc_sma_30_100.example.json` | A 30-day and 100-day SMA trend, plus flash-crash buys and a windowed drawdown stop |
| `btc_ema_10_100.example.json` | The textbook EMA crossover |
| `btc_growth_09.example.json` | A two-of-three vote that reads ADX's `plus_di` and `minus_di` outputs |
| `btc_trend_vote_4in_1out.example.json` | A five-way vote that includes `pct_change(close, '90d')` |

The examples illustrate the syntax. They are not trading recommendations: as shipped, the friction screen blocks every entry of `btc_macro_horizon`, and the other four finish behind buying and holding (see [the templates' results](../config/.example/README.md#backtested-models-a-sneak-peek)).

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
- Reserved names cannot be used: the price fields (`open`, `high`, `low`, `close`, `volume`, `hl2`, `hlc3`, `ohlc4`), the rule function names (`previous`, `highest` and so on), and `True`/`False`. Python keywords such as `and`, `or`, `not` and `if` are rejected too.

### Definition keys

| Key | Required | Meaning |
|---|---|---|
| `type` | yes | One of the indicator types in [section 4](#4-indicator-catalog), in capitals: `"EMA"`, `"BOLLINGER"` and so on. |
| *parameters* | depends | Type-specific settings such as `period`, `std_dev` or `fast`. Parameters with defaults can be left out. |
| `source` | no | The series the indicator is computed from (default `"close"`). This can be a price field or **another indicator's name**, for example `{"type": "SMA", "period": 5, "source": "rsi"}` smooths an RSI. For an indicator with several outputs, name one output: `"source": "macd.line"`. Only types that list `source` accept it. |
| `plot` | no | `false` leaves the indicator off the charts; its values still appear in the hover tooltip. Every indicator is drawn by default: price-scale indicators on the price chart, oscillators in their own panel (see [section 10](#10-charts-and-tooltips)). |
| `label` | no | Display name for the chart legend and reports. Defaults to `name (TYPE period)`, with the period in candles: `"12h"` on 15-minute candles shows as `fast_ema (EMA 48)`. MACD shows its three periods, `macd (MACD 12, 26, 9)`, and types without a period show only the type, `obv (OBV)`. |
| `description` | no | Free text for readers of the profile; ignored by the simulator. |

Unknown keys are rejected, so a typo like `"peroid"` is caught instead of being silently ignored.

### Periods: candles or durations

Every period-like parameter (`period`, `fast`, `slow`, `signal`, `k_period`, `d_period`, `smooth`, `atr_period`) accepts either:

- **A whole number of candles**, at least 1: `30` means 30 candles of the profile's `period_granularity`. `30.0` is accepted; `2.5`, `0` and `true` are errors.
- **A duration string**: a number followed by `m` (minutes), `h` (hours), `d` (days) or `w` (weeks), such as `"90m"`, `"7.5h"`, `"5d"` or `"2w"`. The unit may be upper or lower case, and spaces around it are ignored. It is converted to the nearest whole number of candles; an exact half rounds to the even number, so `"2.5h"` on 1-hour candles is 2 candles and `"7.5h"` is 8.

Durations keep a strategy's meaning when you change candle size. With `FIFTEEN_MINUTE` candles, `"45h"` is 180 candles; with `ONE_HOUR` candles it becomes 45 candles, still spanning 45 hours. A duration that rounds to zero candles, half a candle or less, is an error ("shorter than one candle").

| Granularity | `"1h"` | `"1d"` | `"1w"` |
|---|---|---|---|
| `ONE_MINUTE` | 60 | 1,440 | 10,080 |
| `FIVE_MINUTE` | 12 | 288 | 2,016 |
| `FIFTEEN_MINUTE` | 4 | 96 | 672 |
| `THIRTY_MINUTE` | 2 | 48 | 336 |
| `ONE_HOUR` | 1 | 24 | 168 |
| `TWO_HOUR` | error | 12 | 84 |
| `SIX_HOUR` | error | 4 | 28 |
| `ONE_DAY` | error | 1 | 7 |

### Warm-up

An indicator has no value until it has seen enough history: a 180-period moving average needs 180 candles. Before then its value is *unknown*, shown as "warming up" in the chart tooltip. Rules treat unknown values as "not yet true", so **no trade is entered or exited on a half-formed indicator**. Long periods therefore delay the first possible trade by that much history.

Warm-up lengths, counted as the candles needed for the first value (the first value is on the last of them):

| Type | Candles needed for the first value |
|---|---|
| SMA, WMA, EMA, VWMA, STDDEV, CCI, WILLIAMS_R, ATR, DONCHIAN, BOLLINGER, rolling VWAP | `period` |
| RSI, MFI, ROC, MOMENTUM, VOLATILITY | `period + 1` |
| DEMA | `2 x period - 1` |
| TEMA | `3 x period - 2` |
| HMA | `period + floor(sqrt(period)) - 1` |
| ADX `plus_di`, `minus_di` / `adx` | `period + 1` / `2 x period` |
| MACD `line` / `signal`, `histogram` | `slow` / `slow + signal - 1` |
| STOCHASTIC `k` / `d` | `k_period`, plus `smooth - 1` when `smooth` is above 1 / that plus `d_period - 1` |
| KELTNER `middle` / `upper`, `lower` | `period` / the longer of `period` and `atr_period` |
| OBV, anchored VWAP | 1: they have a value from the first candle |

An indicator computed from another indicator adds the two: an SMA of 5 over a 14-period RSI needs `15 + 5 - 1 = 19` candles.

Profiles in the same simulator run share their indicators. Two profiles that define an indicator with the same type and settings compute it once, even if they give it different names. Slow indicators whose values depend on a fixed number of recent candles are also stored between runs; see [Stored metrics](simulator.md#stored-metrics).

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

The simulator has 24 indicator types. Each entry lists the parameters (with defaults; **required** means there is no default), the outputs, the formula, and how it is commonly read. Indicators with several outputs are referred to as `name.output` in rules, for example `bands.upper` or `macd.signal`.

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
- **Formula:** `WMA(2 x WMA(x, period/2) - WMA(x, period), sqrt(period))`, with `period/2` and `sqrt(period)` rounded down
- **Use:** very smooth and very responsive. A popular alternative to EMA crossovers is to trade the HMA's direction: `hma > previous(hma)`.

#### VWMA: Volume-weighted moving average
- **Parameters:** `period` (required), `source` (`close`)
- **Output:** one value
- **Formula:** `sum(value x volume) / sum(volume)` over the window
- **Use:** gives heavy-volume candles more say. When the VWMA sits above the SMA of the same period, volume is backing the up-moves.

#### VWAP: Volume-weighted average price
- **Parameters:** `anchor` (none) or `period` (none). Give one: `"anchor": "day"` (the only anchor), or a rolling `period`. If you give both, the anchor is used and `period` is ignored. With neither, the profile loads but the run stops with `VWAP needs either "anchor": "day" or a rolling "period"`. VWAP always uses `hlc3` and takes no `source`.
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
- **Formula:** Wilder's RSI, `100 - 100 / (1 + average gain / average loss)`, with gains and losses smoothed by Wilder's method (`a = 1 / period`, seeded with a simple average). With no losses in the average it is 100, and with neither gains nor losses it is 50.
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
- **Outputs:** `k` = `100 x (close - lowest low) / (highest high - lowest low)` over `k_period`, smoothed by an SMA of `smooth` when `smooth` is above 1; `d` = SMA of `k` over `d_period`. When the high and low of the window are equal, `k` is unknown.
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
- **Formula:** `100 - 100 / (1 + positive flow / negative flow)` over the last `period` candles, where money flow is typical price times volume, counted positive when the typical price rose and negative when it fell. With no negative flow it is 100, or unknown if there is no positive flow either.
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
- **Formula:** Wilder's directional movement system. `+DI` and `-DI` are smoothed directional moves divided by ATR, and ADX is the Wilder-smoothed `100 x |+DI - -DI| / (+DI + -DI)`.
- **Use:** ADX measures *how strong* a trend is, not its direction. Above 25 is a trending market (favor trend systems) and below 20 a ranging market (favor mean reversion). `trend.plus_di > trend.minus_di` gives the direction.

```json
"trend": {"type": "ADX", "period": 14}
```

### 4.5 Volume

#### OBV: On-balance volume
- **Parameters:** none
- **Output:** one value, cumulative volume
- **Formula:** a running total, starting at 0 on the first candle, that adds the candle's volume on up-closes and subtracts it on down-closes. An unchanged close adds nothing.
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

`entry_signal` needs at least one rule. `exit_signal` may have none, in which case positions close only through the stops, the profit lock or the drawdown limit (see [section 6](#6-how-signals-become-trades)). A single rule may be written as a string instead of a list: `"any": "fast_ema < slow_ema"`.

### 5.2 Syntax

A rule is a formula that is true or false at each candle. You can use:

| Element | Examples |
|---|---|
| Names | `close`, `rsi`, `bands.upper`, `macd.signal` |
| Numbers | `70`, `0.01`, `-1.5` |
| Constants | `True`, `False` |
| Arithmetic | `+`, `-`, `*`, `/`, unary `-`, parentheses |
| Comparisons | `>`, `>=`, `<`, `<=`, `==`, `!=` |
| Chained comparisons | `30 < rsi < 70` (both must hold) |
| Logic | `and`, `or`, `not` |
| Functions | see below |

Operator precedence follows the usual rules: arithmetic, then comparisons, then `not`, then `and`, then `or`. Use parentheses when in doubt: `(fast_ema - slow_ema) / slow_ema >= 0.01`.

**Counting conditions (votes).** A comparison is worth 1 when it holds and 0 when it doesn't, so comparisons can be added up. This rule means "at least two of these three":

```text
((adx.plus_di > adx.minus_di) + (sma10 > sma100) + (sma5 > sma100)) >= 2
```

Wrap each comparison in parentheses, because comparisons bind more loosely than `+`. If any vote is unknown (still warming up), the whole sum is unknown and the rule doesn't fire, so a vote rule waits for its slowest indicator. The templates `btc_growth_09` and `btc_trend_vote_4in_1out` in [config/.example/](../config/.example/README.md#backtested-models-a-sneak-peek) use this pattern.

Rules can use nothing else. There are no variables, strings (except duration periods in functions), lists, indexing, other operators such as `**` or `%`, or attribute access beyond `indicator.output`. Rules are parsed against a whitelist and never run as Python code, so a profile cannot execute arbitrary commands.

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

The period argument `n` must be a constant: a candle count of at least 1 (`3`) or a quoted duration (`'1d'`). It is optional for `previous`, `change` and `pct_change`, and required for `highest` and `lowest`. Arguments are positional only, so `previous(close, n=2)` is an error. `a`, `b` and `x` can be any expression: `crosses_above(close, bands.lower)`, `previous(fast_ema - slow_ema)`.

**Crosses versus states:** `fast_ema > slow_ema` is true on every candle of an uptrend, while `crosses_above(fast_ema, slow_ema)` is true only on the candle where the trend flips. Use crosses for "act on the change", and states for "stay in while the condition holds".

### 5.4 Unknown values

While an indicator is warming up, its value is unknown. Comparisons with an unknown value are unknown, and an unknown rule never fires. With `and`/`or`, an unknown part counts as false: `rsi < 30 or close > bands.upper` can still fire on the band condition while RSI warms up. `not` of an unknown is still unknown.

Arithmetic with an unknown value is unknown, and so are `abs`, `min` and `max` of one. A function that looks back further than the available history, such as `previous(close, 3)` on the first three candles, is unknown too. Division by zero also gives an unknown value rather than an error, including in `pct_change`.

**When a model is ready to trade.** The simulator also tracks the first candle on which *every* entry rule, in both `all` and `any`, can be evaluated: every indicator a rule reads has a value, and every lookback has enough history. For example, `change(sma_50, '10d')` needs a value of `sma_50` both now and 10 days earlier, `highest(x, n)` needs `x` on all of the last `n` candles, and `crosses_above` needs the previous candle too. Unlike rule evaluation, readiness doesn't let an `or` skip an unknown part, so a rule is ready only once all of its parts are. The model's [buy-and-hold benchmark](simulator.md#buy-and-hold-starts-when-the-model-can-first-buy) starts on that candle, or at the model's first buy if that comes sooner. Scripts can read the candle's start time as the result's `ready_to_trade`, which is `None` if the rules never become ready.

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
2. **Flash crashes**, if `strategy.flash_crash_buy` is set: any resting limit buy the candle traded down to fills at its price and is held until the crash recovers. Until the crash recovers, nothing below sells: stops and profit locks are skipped on every crash candle, including the one it recovers in, and exit rules and the drawdown limit wait for the recovery candle's close (see [flash_crash_buy](model_configuration.md#flash_crash_buy)).
3. **Exits**, if a position is open, checked in priority order:
   1. **Stops**:
      - the hard stop, if `safeguards.use_hard_stop` is true and `exit_signal.hard_stop_trigger` is not `false`, at `entry price x (1 - safeguards.hard_stop_percentage)`
      - the trailing stop, if `exit_signal.trailing_stop` is set, at that fraction below the highest price since entry as of the previous candle
      - with `safeguards.drawdown_window` and `max_drawdown_limit`, the drawdown stop, at the price where the model's value would fall `max_drawdown_limit` below its high in the window

      If the candle's low reaches any of them, the highest one fills, at its price, or at the open if the price gapped below it.
   2. **Profit lock**, if no stop fired: if the candle's high reaches `entry price x (1 + profit_lock_target)`, it sells at the target, or at the open if price gapped above it.
   3. **Exit rules**: if the exit rules hold at the candle's close, it sells at the close. The Trades tab shows the reason `EXIT_SIGNAL`.
4. **Drawdown limit** without a window: if equity has fallen `max_drawdown_limit` from its peak, any position closes and trading pauses. After either kind of drawdown exit, trading resumes only once the entry signal has switched **off** and then fires again.
5. **Entries**, if no position is open, including one closed earlier on the same candle: the entry rules must hold at the close, `max_orders_per_day` must allow it, and the signal must pass the **friction screen** (see [the friction screen](model_configuration.md#5-the-friction-screen-worked-through)). The screen uses `exit_signal.profit_lock_target` as the expected gain, so a model without a profit target, or without `safeguards.minimum_net_profit_gate`, is not screened. With a `drawdown_window`, the model also enters only while its value is far enough above the drawdown floor that the entry's own costs can't trigger the drawdown stop at once. Entries fill at the close, with the reason `ENTRY_SIGNAL`.

Rules see only the current and past candles, so there is no look-ahead: a rule evaluated at a candle's close uses that close and earlier data.

For fill prices, fees, exit reasons and the limits of a backtest, see [How trades are simulated](simulator.md#8-how-trades-are-simulated) in the simulator guide.

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
| `"indicators" must be an object mapping names to indicator definitions` | `indicators` is a list or a value instead of an object. |
| `Indicator 'x' must be an object such as {"type": "EMA", "period": 20}` | A definition is a string or a number instead of an object. |
| `Indicator name '2x' must be letters, digits and underscores, not starting with a digit` | Rename the indicator. Python keywords such as `and` also get this message. |
| `Indicator name 'close' is reserved; pick another name` | The name is a price field, a rule function, `True` or `False`. Rename the indicator. |
| `Indicator 'x' has unknown type 'BOLINGER'; choose one of: ...` | Misspelled `type`. Types are upper case; see section 4. |
| `Indicator 'x' (EMA) needs "period"` | A required parameter is missing. |
| `Indicator 'x' (ADX) does not take smoothing; its parameters are: period` | An unknown or misspelled key. |
| `'x' period '3 fortnights' is not a candle count or a duration; use forms like ...` | Use a whole number, or forms like `'90m'`, `'12h'`, `'5d'`, `'2w'`. |
| `'x' period must be a whole number of candles of at least 1, not 2.5` | A candle count must be a whole number from 1 up. For a fraction of a period, use a duration such as `'2.5h'`. |
| `... is shorter than one candle` | For example `'5m'` on 15-minute candles. Use a longer duration. |
| `Indicator 'x' (MACD) needs fast shorter than slow` | Swap `fast` and `slow`. |
| `Indicator 'x' (VWAP) anchor must be "day" or omitted` | `"day"` is the only anchor. For other spans, use a rolling `period`. |
| `Indicator 'x' has source 'y', which is not a price field or indicator output` | The `source` names nothing in the profile. If `y` has several outputs, the message lists them: use one, such as `macd.line`. |
| `Indicator sources form a loop: a -> b -> a` | Two indicators use each other as `source`. |
| `Rule '...': unknown name 'fsat_ema'; did you mean fast_ema? ...` | A typo in a rule, or an indicator that isn't defined. A multi-output indicator used without `.output`, such as `macd`, also gets this message, suggesting `macd.line`. |
| `Rule '...' is not a valid formula: ...` | A syntax error, such as an unbalanced parenthesis or a dangling operator. |
| `Rule '...': '...' is not allowed; rules may use names, numbers, ...` | Something outside the rule language, such as a list, an index, `**` or a lambda. |
| `Rule '...': 'abc' is not a number` | A quoted string outside a function's period argument. |
| `Rule '...': unknown function 'foo'; available: ...` | A misspelled or unsupported function; see [section 5.3](#53-functions). |
| `Rule '...': highest() takes 2 arguments, got 1` | Wrong number of arguments. `highest` and `lowest` need a period. |
| `Rule '...': previous() takes positional arguments only` | Write `previous(close, 2)`, not `previous(close, n=2)`. |
| `Rule '...': the period in previous() must be a number of candles or a duration like '1d'` | Function periods must be constants, not indicators or expressions. |
| `strategy.entry_signal needs at least one rule in "all" or "any"` | An empty entry block. |
| `strategy.entry_signal.all must be a list of rule formulas` | `all` or `any` is something other than a list or a single string. |
| `strategy.entry_signal.all entries must be formulas in quotes, not 5` | A rule is a number or other value instead of a string. |

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

A profile counts as old-format when its `indicators` has any of `fast_window_periods`, `slow_window_periods` or `emergency_ema_periods`. Missing fields take the old defaults: periods of 30, 180 and 4 candles, `price_source` `"close"`, `fast_ema_gt_slow_ema` and `fast_ema_lt_slow_ema` true, `emergency_ema_gt_fast_ema` false and `minimum_trend_strength` 0. The `emergency_ema` indicator is always defined, even when no rule uses it. If a signal block already has `all` or `any` rules, they are kept and its old fields are ignored. The old `trend_filter` field is no longer used.

---

## 10. Charts and tooltips

Every indicator in the profile gets its own line on the **Chart** tab.

- **Price-scale indicators** (moving averages, VWAP, and the upper, middle and lower lines of Bollinger, Keltner and Donchian) are drawn over the close price. Each indicator has its own color, assigned in the order the indicators are defined; band edges are thinner lines in their indicator's color. There is no limit on how many are drawn: past the fifth, the colors repeat with a dashed line, then a dotted one, so every indicator keeps a distinct color-and-style pair in the legend for up to 15 indicators. Lines with a period under 10 candles hug the price, so they are drawn beneath it; longer ones are drawn on top, slower over faster.
- **Oscillators** (RSI, MACD, Stochastic, ROC, Momentum, CCI, Williams %R, MFI, ATR, standard deviation, volatility, ADX and OBV) each get a **panel of their own** under the price chart, in the order they are defined, because their scales differ from price and from each other. Each panel shows the indicator's label in its top-left corner. Multi-output oscillators draw every output as a separate line in their panel, with a small legend: MACD's `line`, `signal` and `histogram`, ADX's `adx`, `plus_di` and `minus_di`, and Stochastic's `k` and `d`. Bollinger's `bandwidth` and `percent_b` are not drawn anywhere; read them in the tooltip.
- Panels show their conventional reference levels as thin lines: RSI 30/70, MFI and Stochastic 20/80, Williams %R -80/-20, CCI ±100, ADX 20 and 25, and zero for MACD, ROC and Momentum. RSI, MFI, Stochastic and Williams %R keep their fixed scale (0-100, or -100-0); other panels scale to their data.
- All panels share the price chart's time axis: zooming or panning any panel with the toolbar moves them all, and double-clicking a trade in the Trades tab zooms every panel to it, refitting the panels without a fixed scale to the values in view.
- Hovering anywhere shows a crosshair through every panel and a tooltip, in the top corner away from the cursor, listing **every** indicator's value at that candle, including each output of multi-output indicators. A value that is still unknown shows as "warming up".
- Set `"plot": false` on an indicator to leave it off the chart (and remove its panel); its values still appear in the tooltip.
- Buy (▲) and sell (▼) markers show where rules, stops and targets filled. Gray bands mark the periods when the drawdown limit paused trading.
- Each panel takes a slice of the chart's height, so profiles with many oscillators give each panel less room; enlarge the window or hide the less important ones with `"plot": false`.

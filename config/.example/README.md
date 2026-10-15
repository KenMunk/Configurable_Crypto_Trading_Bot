# config/.example/: template profiles

These are public, tracked template profiles. Copy one into `config/` to start a model of your own. They demonstrate the profile syntax; they are **not** trading recommendations.

## The templates

The folder holds six templates. The two below show the profile syntax. The other four are backtested models, described under [Backtested models: a sneak peek](#backtested-models-a-sneak-peek).

### btc_macro_horizon.example.json: EMA trend following

| | |
|---|---|
| Market | `BTC-USD`, 15-minute candles |
| Indicators | Fast EMA (`7.5h`), slow EMA (`45h`), short "emergency" EMA (`1h`) |
| Entry | All three hold: the fast EMA is above the slow EMA, the emergency EMA is above the fast EMA, and the trend is at least 1% strong: `(fast_ema - slow_ema) / slow_ema >= 0.01` |
| Exit | The fast EMA falls below the slow EMA, a 3% hard stop, or an 8% profit lock |
| Money | Starts with 10,000 and commits 25% per trade, with 12 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 5% minimum-net-profit gate, 12% drawdown limit |

As shipped, the 8% target nets only about 4% after friction and tax at the 0.5% fallback fee (about 3.2% at a 0.9% fee), which is below the 5% gate. The friction screen therefore blocks every entry. The [model configuration guide](../../Project_Documentation/model_configuration.md#5-the-friction-screen-worked-through) works through the numbers.

### btc_mean_reversion.example.json: buying oversold dips

| | |
|---|---|
| Market | `BTC-USD`, 1-hour candles |
| Indicators | Bollinger Bands (`1d`, 2 standard deviations), RSI (14), ADX (14), annualized volatility (`7d`), daily VWAP |
| Entry | All hold: the close is below the lower band, RSI is under 30, ADX is under 20 (a ranging market), and volatility is under 0.8 |
| Exit | The close rises above the middle band or RSI crosses above 60; also a 4% hard stop or a 5% profit lock |
| Money | Starts with 5,000 and commits 25% per trade, with 6 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 1% minimum-net-profit gate, 15% drawdown limit |

## Backtested models: a sneak peek

The four templates below are real models from the project's own research, published because they are among the **weaker** ones: each finishes behind simply buying and holding BTC. They show what the simulator reports and how varied a profile can be, from a two-line moving-average crossover to a five-way trend vote.

Results from one simulator run on 2026-10-15 (UTC) over the previous 10 years, with no start date: 15-minute candles for `btc_sma_30_100` and `btc_ema_10_100`, daily candles for the other two. They charged a 0.9% fee per fill (the live fee tier at the time), 0.25% slippage and 31.3% tax on each year's net gains. Buy and hold starts on the first candle each model could buy on, and pays no fees or taxes.

| Template | Candles | Start → end | Return | Buy & hold | vs Buy & hold | Max drawdown | Trades (win rate) |
|---|---|---|---|---|---|---|---|
| `btc_sma_30_100` | 15-minute | 10,000 → 15,282 | +52.8% | +8,636% | −98.3% | 16.5% | 27 (56%) |
| `btc_ema_10_100` | 15-minute | 2,000 → 84,254 | +4,113% | +8,636% | −51.8% | 71.0% | 20 (35%) |
| `btc_growth_09` | daily | 10,000 → 705,006 | +6,950% | +8,659% | −19.5% | 63.5% | 25 (40%) |
| `btc_trend_vote_4in_1out` | daily | 10,000 → 589,071 | +5,791% | +6,209% | −6.6% | 65.4% | 15 (53%) |

**vs Buy & hold** compares the ending values: −51.8% means the model ended with about half of what buying and holding would have. Results depend on your fee tier and on the candles available when you run them, so your figures will differ. These are past results on one asset, not predictions.

### btc_sma_30_100.example.json: cautious SMA trend with flash-crash buys

| | |
|---|---|
| Market | `BTC-USD`, 15-minute candles |
| Indicators | 30-day and 100-day SMAs |
| Entry | The 30-day SMA is above the 100-day SMA |
| Exit | The 30-day SMA falls below the 100-day SMA, or a 50% profit lock |
| Money | Starts with 10,000 and commits only 20% per trade, with 12 orders per day at most; a separate 100 reserve rests as limit buys of 25 each, 30%, 60%, 90% and 99% below the previous candle's close |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 5% minimum-net-profit gate; sells and pauses when the model's value falls 10% below its 180-day high |

The safest-looking and the weakest result. Its drawdown stays small, but committing 20% per trade, capping winners at +50% and pausing after a 10% dip keep it out of most of Bitcoin's rise. It is the template to read for [flash-crash buys](../../Project_Documentation/model_configuration.md#flash_crash_buy) and the windowed drawdown stop; its 4 flash buys appear in the Metrics tab.

### btc_ema_10_100.example.json: all-in EMA crossover

| | |
|---|---|
| Market | `BTC-USD`, 15-minute candles |
| Indicators | 10-day and 100-day EMAs |
| Entry | The 10-day EMA is above the 100-day EMA |
| Exit | The 10-day EMA falls below the 100-day EMA (the +2,000% profit lock never triggers) |
| Money | Starts with 2,000 and commits 100% per trade, with 12 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 5% minimum-net-profit gate (the +2,000% target always clears it); no hard stop; 50% drawdown limit |

The textbook trend follower. It catches the big trends, but whipsaws in sideways markets (a 35% win rate). Those losing trades, plus 16,847 in fees and 38,851 in yearly taxes, leave it with about half of buy and hold's ending value.

### btc_growth_09.example.json: two-of-three vote with ADX

| | |
|---|---|
| Market | `BTC-USD`, daily candles |
| Indicators | ADX (14), and 5-day, 10-day and 100-day SMAs |
| Entry | At least 2 of 3 hold: `+DI` is above `-DI`, the 10-day SMA is above the 100-day SMA, and the 5-day SMA is above the 100-day SMA |
| Exit | 1 or fewer of the 3 still hold |
| Money | Starts with 10,000 and commits 100% per trade, with 12 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax; no stops, profit lock, minimum-net-profit gate or drawdown limit |

It shows how to count conditions: in a rule, each true comparison counts as 1, so `(a > b) + (c > d) + (e > f) >= 2` reads "at least two of these". It also reads a multi-output indicator (`adx.plus_di`, `adx.minus_di`).

### btc_trend_vote_4in_1out.example.json: five-way trend vote

| | |
|---|---|
| Market | `BTC-USD`, daily candles |
| Indicators | 5-day, 100-day and 140-day SMAs; 10-day and 100-day EMAs |
| Entry | At least 4 of 5 votes agree: the close is above the 100-day SMA, the close is above the 140-day SMA, the 5-day SMA is above the 100-day SMA, the 10-day EMA is above the 100-day EMA, and the 90-day return (`pct_change(close, '90d')`) is positive |
| Exit | 1 or fewer of the 5 votes remain |
| Money | Starts with 10,000 and commits 100% per trade, with 12 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax; no stops, profit lock, minimum-net-profit gate or drawdown limit |

The closest of the four to buy and hold. The gap between its entry bar (4 votes) and its exit bar (1 vote) makes it slow to sell, so it trades only 15 times in 10 years. Its 140-day SMA also takes longer to warm up than the others' 100-day lines, so it can first buy later (2017-03-03), and its buy-and-hold benchmark starts later too.

## Using a template

Copy the template into `config/`, dropping `.example` from the name, and edit the copy:

```bash
cp config/.example/btc_mean_reversion.example.json config/btc_mean_reversion.json
```

The simulator lists only files directly in `config/`, so the templates here never show up as models.

## Rules for this folder

- **Everything here is public.** Never add private strategies or secrets.
- **Every template must be valid.** `tests/test_rules.py` (`test_all_example_profiles_are_valid`) loads every `*.json` file here, so a broken template fails the test suite.
- Keep the `.example.json` suffix, so that templates are easy to tell apart from working copies.

For every field, see the [model configuration guide](../../Project_Documentation/model_configuration.md).

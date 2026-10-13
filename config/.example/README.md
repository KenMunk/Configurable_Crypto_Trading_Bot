# config/.example/: template profiles

These are public, tracked template profiles. Copy one into `config/` to start a model of your own. They demonstrate the profile syntax; they are **not** trading recommendations.

## The templates

### btc_macro_horizon.example.json: EMA trend following

| | |
|---|---|
| Market | `BTC-USD`, 15-minute candles |
| Indicators | Fast EMA (`7.5h`), slow EMA (`45h`), short "emergency" EMA (`1h`) |
| Entry | All three hold: the fast EMA is above the slow EMA, the emergency EMA is above the fast EMA, and the trend is at least 1% strong: `(fast_ema - slow_ema) / slow_ema >= 0.01` |
| Exit | The fast EMA falls below the slow EMA, a 3% hard stop, or an 8% profit lock |
| Money | Starts with 10,000 and commits 25% per trade, with 12 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 5% minimum-net-profit gate, 12% drawdown limit |

As shipped, the 8% target nets only about 4% after friction and tax, which is below the 5% gate. The friction screen therefore blocks every entry. The [model configuration guide](../../Project_Documentation/model_configuration.md#5-the-friction-screen-worked-through) works through the numbers.

### btc_mean_reversion.example.json: buying oversold dips

| | |
|---|---|
| Market | `BTC-USD`, 1-hour candles |
| Indicators | Bollinger Bands (`1d`, 2 standard deviations), RSI (14), ADX (14), annualized volatility (`7d`), daily VWAP |
| Entry | All hold: the close is below the lower band, RSI is under 30, ADX is under 20 (a ranging market), and volatility is under 0.8 |
| Exit | The close rises above the middle band or RSI crosses above 60; also a 4% hard stop or a 5% profit lock |
| Money | Starts with 5,000 and commits 25% per trade, with 6 orders per day at most |
| Costs and risk | Live fee tier (fallback 0.5%), 0.25% slippage, 31.3% tax, 1% minimum-net-profit gate, 15% drawdown limit |

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

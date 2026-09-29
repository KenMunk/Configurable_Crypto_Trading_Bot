import math

import numpy as np
import pytest

from indicators import INDICATORS, IndicatorContext, IndicatorError, output_names, parse_period

FIFTEEN_MINUTES = 900


def _candles(count=400, seed=7):
    """A deterministic random walk with realistic high/low/volume."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, count)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.005, count))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.005, count))
    volume = rng.uniform(1, 10, count)
    return [
        {"start": i * FIFTEEN_MINUTES, "open": o, "high": h, "low": l, "close": c, "volume": v}
        for i, (o, h, l, c, v) in enumerate(zip(open_, high, low, close, volume))
    ]


def _compute(definition, name="x", candles=None):
    candles = candles or _candles()
    context = IndicatorContext(candles, {name: definition}, FIFTEEN_MINUTES)
    return {output: context.series(output) for output in output_names({name: definition})}, candles


def _column(candles, key):
    return np.array([c[key] for c in candles])


def _reference_ema(values, period):
    out, value = [], values[0]
    for v in values:
        value += 2 / (period + 1) * (v - value)
        out.append(value)
    out = np.array(out)
    out[:period - 1] = np.nan
    return out


def _reference_wilder(values, period):
    out = np.full(len(values), np.nan)
    value = np.mean(values[:period])
    out[period - 1] = value
    for i in range(period, len(values)):
        value += (values[i] - value) / period
        out[i] = value
    return out


def _assert_close(actual, expected):
    np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-9, equal_nan=True)


def test_parse_period_accepts_candles_and_durations():
    assert parse_period(30, FIFTEEN_MINUTES) == 30
    assert parse_period("12h", FIFTEEN_MINUTES) == 48
    assert parse_period("5d", FIFTEEN_MINUTES) == 480
    assert parse_period("90m", FIFTEEN_MINUTES) == 6
    assert parse_period("2w", 86_400) == 14
    for bad in (0, 2.5, "3 fortnights", "5m", True):
        with pytest.raises(IndicatorError):
            parse_period(bad, FIFTEEN_MINUTES)


def test_sma_and_wma_match_reference_windows():
    values, candles = _compute({"type": "SMA", "period": 20})
    close = _column(candles, "close")
    expected = np.array([np.nan] * 19 + [close[i - 19:i + 1].mean() for i in range(19, len(close))])
    _assert_close(values["x"], expected)

    values, _ = _compute({"type": "WMA", "period": 10}, candles=candles)
    weights = np.arange(1, 11)
    expected = np.array([np.nan] * 9 + [np.dot(close[i - 9:i + 1], weights) / weights.sum()
                                        for i in range(9, len(close))])
    _assert_close(values["x"], expected)


def test_ema_family_matches_reference_recursion():
    candles = _candles()
    close = _column(candles, "close")
    ema = _reference_ema(close, 12)
    _assert_close(_compute({"type": "EMA", "period": 12}, candles=candles)[0]["x"], ema)

    ema_of_ema = _reference_ema(np.nan_to_num(ema, nan=0.0), 12)  # shape check only below
    dema = _compute({"type": "DEMA", "period": 12}, candles=candles)[0]["x"]
    assert np.isnan(dema[:22]).all() and not np.isnan(dema[22:]).any()
    assert ema_of_ema.shape == dema.shape


def test_rsi_matches_wilder_reference_and_stays_in_range():
    candles = _candles()
    close = _column(candles, "close")
    change = np.diff(close)
    gains = _reference_wilder(np.where(change > 0, change, 0.0), 14)
    losses = _reference_wilder(np.where(change < 0, -change, 0.0), 14)
    expected = np.concatenate([[np.nan], 100 - 100 / (1 + gains / losses)])

    rsi = _compute({"type": "RSI", "period": 14}, candles=candles)[0]["x"]

    _assert_close(rsi, expected)
    assert np.nanmin(rsi) >= 0 and np.nanmax(rsi) <= 100


def test_atr_matches_wilder_average_of_true_range():
    candles = _candles()
    high, low, close = (_column(candles, key) for key in ("high", "low", "close"))
    true_range = np.maximum.reduce([high - low, np.abs(high - np.roll(close, 1)), np.abs(low - np.roll(close, 1))])
    true_range[0] = high[0] - low[0]

    _assert_close(_compute({"type": "ATR", "period": 14}, candles=candles)[0]["x"],
                  _reference_wilder(true_range, 14))


def test_bollinger_bands_use_population_standard_deviation():
    values, candles = _compute({"type": "BOLLINGER", "period": 20, "std_dev": 2})
    close = _column(candles, "close")
    i = 250
    window = close[i - 19:i + 1]
    assert values["x.middle"][i] == pytest.approx(window.mean())
    assert values["x.upper"][i] == pytest.approx(window.mean() + 2 * window.std())
    assert values["x.lower"][i] == pytest.approx(window.mean() - 2 * window.std())
    assert values["x.percent_b"][i] == pytest.approx((close[i] - values["x.lower"][i])
                                                     / (values["x.upper"][i] - values["x.lower"][i]))


def test_macd_line_signal_and_histogram():
    values, candles = _compute({"type": "MACD", "fast": 12, "slow": 26, "signal": 9})
    close = _column(candles, "close")
    line = _reference_ema(close, 12) - _reference_ema(close, 26)
    _assert_close(values["x.line"], line)
    _assert_close(values["x.histogram"], values["x.line"] - values["x.signal"])


def test_stochastic_williams_and_donchian_windows():
    candles = _candles()
    high, low, close = (_column(candles, key) for key in ("high", "low", "close"))
    i = 300
    highest, lowest = high[i - 13:i + 1].max(), low[i - 13:i + 1].min()

    stochastic = _compute({"type": "STOCHASTIC", "k_period": 14, "d_period": 3}, candles=candles)[0]
    williams = _compute({"type": "WILLIAMS_R", "period": 14}, candles=candles)[0]["x"]
    donchian = _compute({"type": "DONCHIAN", "period": 14}, candles=candles)[0]

    assert stochastic["x.k"][i] == pytest.approx(100 * (close[i] - lowest) / (highest - lowest))
    assert stochastic["x.d"][i] == pytest.approx(stochastic["x.k"][i - 2:i + 1].mean())
    assert williams[i] == pytest.approx(-100 * (highest - close[i]) / (highest - lowest))
    assert (donchian["x.upper"][i], donchian["x.lower"][i]) == (highest, lowest)


def test_volume_indicators():
    candles = _candles()
    close, volume = _column(candles, "close"), _column(candles, "volume")
    high, low = _column(candles, "high"), _column(candles, "low")
    typical = (high + low + close) / 3

    vwma = _compute({"type": "VWMA", "period": 10}, candles=candles)[0]["x"]
    rolling_vwap = _compute({"type": "VWAP", "period": 10}, candles=candles)[0]["x"]
    obv = _compute({"type": "OBV"}, candles=candles)[0]["x"]

    assert vwma[50] == pytest.approx(np.dot(close[41:51], volume[41:51]) / volume[41:51].sum())
    assert rolling_vwap[50] == pytest.approx(np.dot(typical[41:51], volume[41:51]) / volume[41:51].sum())
    expected_obv = np.cumsum(np.concatenate([[0], np.sign(np.diff(close))]) * volume)
    _assert_close(obv, expected_obv)


def test_daily_vwap_resets_each_utc_day():
    candles = _candles(200)
    values = _compute({"type": "VWAP", "anchor": "day"}, candles=candles)[0]["x"]
    day_start = 96  # 96 fifteen-minute candles per day
    first = candles[day_start]
    assert values[day_start] == pytest.approx((first["high"] + first["low"] + first["close"]) / 3)


def test_trend_and_volatility_indicators_are_bounded_and_warm_up():
    candles = _candles()
    adx = _compute({"type": "ADX", "period": 14}, candles=candles)[0]
    for output in ("x.adx", "x.plus_di", "x.minus_di"):
        assert 0 <= np.nanmin(adx[output]) and np.nanmax(adx[output]) <= 100
    assert np.isnan(adx["x.adx"][:27]).all() and not np.isnan(adx["x.adx"][28:]).any()

    mfi = _compute({"type": "MFI", "period": 14}, candles=candles)[0]["x"]
    cci = _compute({"type": "CCI", "period": 20}, candles=candles)[0]["x"]
    assert 0 <= np.nanmin(mfi) and np.nanmax(mfi) <= 100
    assert np.isnan(cci[:19]).all() and not np.isnan(cci[19:]).any()

    volatility = _compute({"type": "VOLATILITY", "period": 96}, candles=candles)[0]["x"]
    close = _column(candles, "close")
    log_returns = np.diff(np.log(close))[-96:]
    assert volatility[-1] == pytest.approx(log_returns.std() * math.sqrt(365 * 96))


def test_indicators_can_use_another_indicator_as_their_source():
    candles = _candles()
    definitions = {"rsi": {"type": "RSI", "period": 14}, "rsi_smooth": {"type": "SMA", "period": 5, "source": "rsi"}}
    context = IndicatorContext(candles, definitions, FIFTEEN_MINUTES)
    rsi, smooth = context.series("rsi"), context.series("rsi_smooth")
    assert smooth[100] == pytest.approx(rsi[96:101].mean())


def test_source_loops_and_bad_references_are_reported():
    definitions = {"a": {"type": "SMA", "period": 5, "source": "b"}, "b": {"type": "EMA", "period": 5, "source": "a"}}
    with pytest.raises(IndicatorError, match="loop"):
        IndicatorContext(_candles(), definitions, FIFTEEN_MINUTES).series("a")
    with pytest.raises(IndicatorError, match="macd.line"):
        IndicatorContext(_candles(), {"macd": {"type": "MACD"}}, FIFTEEN_MINUTES).series("macd")


def test_every_indicator_type_computes_on_real_shaped_data():
    candles = _candles()
    for indicator_type, (_, parameters, outputs, _, _) in INDICATORS.items():
        definition = {"type": indicator_type}
        if "period" in parameters and indicator_type != "VWAP":
            definition["period"] = 10
        if indicator_type == "VWAP":
            definition["anchor"] = "day"
        values, _ = _compute(definition, candles=candles)
        for output, series in values.items():
            assert len(series) == len(candles), output
            assert not np.isnan(series[-1]), f"{indicator_type} {output} never warmed up"

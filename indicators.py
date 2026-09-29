"""Technical indicator library for model configuration files.

Each profile names its indicators and picks a type from INDICATORS, for example
    "slow_ema": {"type": "EMA", "period": 180}
    "bands":    {"type": "BOLLINGER", "period": "5d", "std_dev": 2}

Every indicator is computed once over the whole candle history as a numpy array.
Values are NaN until the indicator has enough history (its warm-up), and rules
treat NaN as "not yet known", so nothing trades on a half-formed indicator.
See Project_Documentation/indicator_reference.md for the full reference.
"""

import math
import re

import numpy as np

PRICE_FIELDS = {
    "open": "Candle open price",
    "high": "Candle high price",
    "low": "Candle low price",
    "close": "Candle close price",
    "volume": "Candle volume in the base currency",
    "hl2": "(high + low) / 2",
    "hlc3": "(high + low + close) / 3, the typical price",
    "ohlc4": "(open + high + low + close) / 4",
}
DURATION_SECONDS = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
SECONDS_PER_YEAR = 365 * 86_400
REQUIRED = object()


class IndicatorError(ValueError):
    pass


def parse_period(value, granularity_seconds, name="period"):
    """Convert a period (whole candles, or a duration such as "12h") to a candle count."""
    if isinstance(value, bool):
        raise IndicatorError(f"{name} must be a number of candles or a duration like '12h', not {value!r}")
    if isinstance(value, (int, float)):
        if value != int(value) or value < 1:
            raise IndicatorError(f"{name} must be a whole number of candles of at least 1, not {value!r}")
        return int(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", str(value).lower())
    if not match:
        raise IndicatorError(
            f"{name} {value!r} is not a candle count or a duration; use forms like 30, '90m', '12h', '5d' or '2w'"
        )
    candles = round(float(match[1]) * DURATION_SECONDS[match[2]] / granularity_seconds)
    if candles < 1:
        raise IndicatorError(f"{name} {value!r} is shorter than one candle")
    return candles


# --- numeric building blocks ---------------------------------------------------------

def _first_valid(x):
    valid = np.flatnonzero(~np.isnan(x))
    return int(valid[0]) if valid.size else len(x)


def _nan_like(x):
    return np.full(len(x), np.nan)


def shift(x, periods):
    """The value `periods` candles earlier; NaN where that is before the first candle."""
    out = _nan_like(x)
    if periods < len(x):
        out[periods:] = x[:len(x) - periods]
    return out


def _recursive(x, alpha, seed_mean_periods=None):
    """Exponential smoothing: value += alpha * (x - value), starting at the first valid value.

    With seed_mean_periods, the first value is the simple mean of that many values
    (Wilder's convention for RSI, ATR and ADX).
    """
    out = _nan_like(x)
    start = _first_valid(x)
    values = x.tolist()
    if seed_mean_periods:
        seed_end = start + seed_mean_periods
        if seed_end > len(x):
            return out
        value = sum(values[start:seed_end]) / seed_mean_periods
        out[seed_end - 1] = value
        start = seed_end
    elif start < len(x):
        value = values[start]
    result = out.tolist()
    for i in range(start, len(x)):
        v = values[i]
        if v == v:  # skip NaN gaps
            value += alpha * (v - value)
        result[i] = value
    return np.array(result)


def ema(x, periods):
    out = _recursive(x, 2 / (periods + 1))
    start = _first_valid(x)
    out[start:start + periods - 1] = np.nan  # warm-up
    return out


def wilder(x, periods):
    return _recursive(x, 1 / periods, seed_mean_periods=periods)


def rolling_sum(x, periods):
    out = _nan_like(x)
    start = _first_valid(x)
    values = x[start:]
    if len(values) < periods:
        return out
    offset = values[0]  # subtracting a constant keeps the cumulative sum precise
    totals = np.concatenate([[0.0], np.cumsum(values - offset)])
    out[start + periods - 1:] = totals[periods:] - totals[:-periods] + offset * periods
    return out


def sma(x, periods):
    return rolling_sum(x, periods) / periods


def rolling_std(x, periods):
    """Population standard deviation over the window, as Bollinger Bands use."""
    mean = sma(x, periods)
    start = _first_valid(x)
    centered = x - (x[start] if start < len(x) else 0.0)
    variance = sma(centered * centered, periods) - (mean - (x[start] if start < len(x) else 0.0)) ** 2
    return np.sqrt(np.maximum(variance, 0.0))


def _rolling_reduce(x, periods, reducer):
    out = _nan_like(x)
    start = _first_valid(x)
    values = x[start:]
    if len(values) >= periods:
        out[start + periods - 1:] = reducer(np.lib.stride_tricks.sliding_window_view(values, periods), axis=1)
    return out


def rolling_max(x, periods):
    return _rolling_reduce(x, periods, np.max)


def rolling_min(x, periods):
    return _rolling_reduce(x, periods, np.min)


def wma(x, periods):
    out = _nan_like(x)
    start = _first_valid(x)
    values = x[start:]
    if len(values) >= periods:
        weights = np.arange(periods, 0, -1, dtype=float)  # newest value weighs most
        out[start + periods - 1:] = np.convolve(values, weights, mode="valid") / weights.sum()
    return out


def _mean_absolute_deviation(x, periods, chunk=20_000):
    out = _nan_like(x)
    start = _first_valid(x)
    values = x[start:]
    if len(values) < periods:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values, periods)
    result = np.empty(len(windows))
    for begin in range(0, len(windows), chunk):  # chunked so long windows don't allocate gigabytes
        block = windows[begin:begin + chunk]
        result[begin:begin + chunk] = np.abs(block - block.mean(axis=1, keepdims=True)).mean(axis=1)
    out[start + periods - 1:] = result
    return out


def _true_range(ctx):
    previous_close = shift(ctx.close, 1)
    ranges = np.vstack([ctx.high - ctx.low, np.abs(ctx.high - previous_close), np.abs(ctx.low - previous_close)])
    true_range = np.nanmax(ranges, axis=0)
    true_range[0] = ctx.high[0] - ctx.low[0]
    return true_range


def _divide(numerator, denominator):
    with np.errstate(divide="ignore", invalid="ignore"):
        result = numerator / denominator
    result[~np.isfinite(result)] = np.nan
    return result


# --- indicator implementations ----------------------------------------------------------
# Each takes the evaluation context and resolved parameters, and returns one array or a
# dict of named output arrays.

def _sma(ctx, p):
    return sma(ctx.source(p["source"]), p["period"])


def _ema(ctx, p):
    return ema(ctx.source(p["source"]), p["period"])


def _wma(ctx, p):
    return wma(ctx.source(p["source"]), p["period"])


def _dema(ctx, p):
    first = ema(ctx.source(p["source"]), p["period"])
    return 2 * first - ema(first, p["period"])


def _tema(ctx, p):
    first = ema(ctx.source(p["source"]), p["period"])
    second = ema(first, p["period"])
    return 3 * first - 3 * second + ema(second, p["period"])


def _hma(ctx, p):
    x, n = ctx.source(p["source"]), p["period"]
    raw = 2 * wma(x, max(n // 2, 1)) - wma(x, n)
    return wma(raw, max(int(math.sqrt(n)), 1))


def _vwma(ctx, p):
    x = ctx.source(p["source"])
    return _divide(rolling_sum(x * ctx.volume, p["period"]), rolling_sum(ctx.volume, p["period"]))


def _vwap(ctx, p):
    typical = ctx.series("hlc3")
    if p["anchor"] == "day":
        day = ctx.time // 86_400
        new_day = np.concatenate([[True], day[1:] != day[:-1]])
        group = np.cumsum(new_day) - 1
        starts = np.flatnonzero(new_day)
        weighted = np.cumsum(typical * ctx.volume)
        volume = np.cumsum(ctx.volume)
        weighted_before = np.concatenate([[0.0], weighted])[starts][group]
        volume_before = np.concatenate([[0.0], volume])[starts][group]
        return _divide(weighted - weighted_before, volume - volume_before)
    if p["period"] is None:
        raise IndicatorError("VWAP needs either \"anchor\": \"day\" or a rolling \"period\"")
    return _divide(rolling_sum(typical * ctx.volume, p["period"]), rolling_sum(ctx.volume, p["period"]))


def _rsi(ctx, p):
    change = np.diff(ctx.source(p["source"]), prepend=np.nan)
    up = np.where(change > 0, change, 0.0)
    down = np.where(change < 0, -change, 0.0)
    up[np.isnan(change)] = down[np.isnan(change)] = np.nan  # no change before the first value
    gains, losses = wilder(up, p["period"]), wilder(down, p["period"])
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + gains / losses)
    rsi[(losses == 0) & (gains > 0)] = 100.0
    rsi[(losses == 0) & (gains == 0)] = 50.0
    return rsi


def _macd(ctx, p):
    x = ctx.source(p["source"])
    line = ema(x, p["fast"]) - ema(x, p["slow"])
    signal = ema(line, p["signal"])
    return {"line": line, "signal": signal, "histogram": line - signal}


def _stochastic(ctx, p):
    highest = rolling_max(ctx.high, p["k_period"])
    lowest = rolling_min(ctx.low, p["k_period"])
    k = 100 * _divide(ctx.close - lowest, highest - lowest)
    if p["smooth"] > 1:
        k = sma(k, p["smooth"])
    return {"k": k, "d": sma(k, p["d_period"])}


def _roc(ctx, p):
    x = ctx.source(p["source"])
    return 100 * (_divide(x, shift(x, p["period"])) - 1)


def _momentum(ctx, p):
    x = ctx.source(p["source"])
    return x - shift(x, p["period"])


def _cci(ctx, p):
    typical = ctx.series("hlc3")
    return _divide(typical - sma(typical, p["period"]), 0.015 * _mean_absolute_deviation(typical, p["period"]))


def _williams_r(ctx, p):
    highest = rolling_max(ctx.high, p["period"])
    lowest = rolling_min(ctx.low, p["period"])
    return -100 * _divide(highest - ctx.close, highest - lowest)


def _atr(ctx, p):
    return wilder(_true_range(ctx), p["period"])


def _bollinger(ctx, p):
    x = ctx.source(p["source"])
    middle = sma(x, p["period"])
    width = p["std_dev"] * rolling_std(x, p["period"])
    upper, lower = middle + width, middle - width
    return {
        "upper": upper, "middle": middle, "lower": lower,
        "bandwidth": _divide(upper - lower, middle),
        "percent_b": _divide(x - lower, upper - lower),
    }


def _keltner(ctx, p):
    middle = ema(ctx.series("hlc3"), p["period"])
    width = p["multiplier"] * wilder(_true_range(ctx), p["atr_period"])
    return {"upper": middle + width, "middle": middle, "lower": middle - width}


def _donchian(ctx, p):
    upper = rolling_max(ctx.high, p["period"])
    lower = rolling_min(ctx.low, p["period"])
    return {"upper": upper, "middle": (upper + lower) / 2, "lower": lower}


def _stddev(ctx, p):
    return rolling_std(ctx.source(p["source"]), p["period"])


def _volatility(ctx, p):
    x = ctx.source(p["source"])
    with np.errstate(divide="ignore", invalid="ignore"):
        log_returns = np.log(_divide(x, shift(x, 1)))
    deviation = rolling_std(log_returns, p["period"])
    if p["annualize"]:
        deviation = deviation * math.sqrt(SECONDS_PER_YEAR / ctx.granularity_seconds)
    return deviation


def _adx(ctx, p):
    n = p["period"]
    up = np.diff(ctx.high, prepend=np.nan)
    down = -np.diff(ctx.low, prepend=np.nan)
    plus_move = np.where((up > down) & (up > 0), up, 0.0)
    minus_move = np.where((down > up) & (down > 0), down, 0.0)
    plus_move[0] = minus_move[0] = np.nan
    true_range = _true_range(ctx)
    true_range[0] = np.nan
    smoothed_range = wilder(true_range, n)
    plus_di = 100 * _divide(wilder(plus_move, n), smoothed_range)
    minus_di = 100 * _divide(wilder(minus_move, n), smoothed_range)
    dx = 100 * _divide(np.abs(plus_di - minus_di), plus_di + minus_di)
    return {"adx": wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di}


def _obv(ctx, p):
    direction = np.sign(np.diff(ctx.close, prepend=ctx.close[0]))
    return np.cumsum(direction * ctx.volume)


def _mfi(ctx, p):
    typical = ctx.series("hlc3")
    flow = typical * ctx.volume
    change = np.diff(typical, prepend=np.nan)
    positive = rolling_sum(np.where(change > 0, flow, 0.0), p["period"])
    negative = rolling_sum(np.where(change < 0, flow, 0.0), p["period"])
    positive[: p["period"]] = np.nan  # the first candle has no prior price to compare with
    with np.errstate(divide="ignore", invalid="ignore"):
        mfi = 100 - 100 / (1 + positive / negative)
    mfi[(negative == 0) & (positive > 0)] = 100.0
    return mfi


SOURCE = {"source": "close"}

# type: (function, parameters with defaults, outputs or None for one value, plotted on price chart?, summary)
INDICATORS = {
    "SMA": (_sma, {"period": REQUIRED, **SOURCE}, None, True, "Simple moving average"),
    "EMA": (_ema, {"period": REQUIRED, **SOURCE}, None, True, "Exponential moving average"),
    "WMA": (_wma, {"period": REQUIRED, **SOURCE}, None, True, "Linearly weighted moving average"),
    "DEMA": (_dema, {"period": REQUIRED, **SOURCE}, None, True, "Double exponential moving average"),
    "TEMA": (_tema, {"period": REQUIRED, **SOURCE}, None, True, "Triple exponential moving average"),
    "HMA": (_hma, {"period": REQUIRED, **SOURCE}, None, True, "Hull moving average"),
    "VWMA": (_vwma, {"period": REQUIRED, **SOURCE}, None, True, "Volume-weighted moving average"),
    "VWAP": (_vwap, {"period": None, "anchor": None}, None, True, "Volume-weighted average price"),
    "RSI": (_rsi, {"period": 14, **SOURCE}, None, False, "Relative strength index (0-100)"),
    "MACD": (_macd, {"fast": 12, "slow": 26, "signal": 9, **SOURCE}, ["line", "signal", "histogram"], False,
             "Moving average convergence/divergence"),
    "STOCHASTIC": (_stochastic, {"k_period": 14, "d_period": 3, "smooth": 1}, ["k", "d"], False,
                   "Stochastic oscillator (0-100)"),
    "ROC": (_roc, {"period": REQUIRED, **SOURCE}, None, False, "Rate of change, percent"),
    "MOMENTUM": (_momentum, {"period": REQUIRED, **SOURCE}, None, False, "Price change over the period"),
    "CCI": (_cci, {"period": 20}, None, False, "Commodity channel index"),
    "WILLIAMS_R": (_williams_r, {"period": 14}, None, False, "Williams %R (-100 to 0)"),
    "ATR": (_atr, {"period": 14}, None, False, "Average true range, in price units"),
    "BOLLINGER": (_bollinger, {"period": 20, "std_dev": 2.0, **SOURCE},
                  ["upper", "middle", "lower", "bandwidth", "percent_b"], True, "Bollinger Bands"),
    "KELTNER": (_keltner, {"period": 20, "atr_period": 10, "multiplier": 2.0}, ["upper", "middle", "lower"], True,
                "Keltner Channels"),
    "DONCHIAN": (_donchian, {"period": 20}, ["upper", "middle", "lower"], True, "Donchian Channels"),
    "STDDEV": (_stddev, {"period": REQUIRED, **SOURCE}, None, False, "Rolling standard deviation, in price units"),
    "VOLATILITY": (_volatility, {"period": REQUIRED, "annualize": True, **SOURCE}, None, False,
                   "Historical volatility of log returns"),
    "ADX": (_adx, {"period": 14}, ["adx", "plus_di", "minus_di"], False, "Average directional index"),
    "OBV": (_obv, {}, None, False, "On-balance volume"),
    "MFI": (_mfi, {"period": 14}, None, False, "Money flow index (0-100)"),
}
PERIOD_PARAMETERS = {"period", "fast", "slow", "signal", "k_period", "d_period", "smooth", "atr_period"}
# Output fields on the price scale, for indicators whose other fields are not prices.
PRICE_SCALE_FIELDS = {"BOLLINGER": ["upper", "middle", "lower"]}
# Chart guides for oscillator panels: conventional reference levels, and fixed axis
# bounds for oscillators that live on a fixed scale.
PANEL_GUIDES = {
    "RSI": {"levels": [30, 70], "bounds": (0, 100)},
    "MFI": {"levels": [20, 80], "bounds": (0, 100)},
    "STOCHASTIC": {"levels": [20, 80], "bounds": (0, 100)},
    "WILLIAMS_R": {"levels": [-80, -20], "bounds": (-100, 0)},
    "ADX": {"levels": [20, 25]},
    "CCI": {"levels": [-100, 100]},
    "MACD": {"levels": [0]},
    "ROC": {"levels": [0]},
    "MOMENTUM": {"levels": [0]},
}


class IndicatorContext:
    """Candle arrays plus lazily computed indicators, resolving sources between them."""

    def __init__(self, candles, definitions, granularity_seconds):
        self.time = np.array([c["start"] for c in candles], dtype=np.int64)
        self.open = np.array([c["open"] for c in candles], dtype=float)
        self.high = np.array([c["high"] for c in candles], dtype=float)
        self.low = np.array([c["low"] for c in candles], dtype=float)
        self.close = np.array([c["close"] for c in candles], dtype=float)
        self.volume = np.array([c["volume"] for c in candles], dtype=float)
        self.granularity_seconds = granularity_seconds
        self.definitions = definitions
        self.values = {}
        self._computing = []

    def series(self, name):
        """A price field or an indicator output, computing indicators on demand."""
        if name in self.values:
            return self.values[name]
        if name in PRICE_FIELDS:
            derived = {
                "hl2": lambda: (self.high + self.low) / 2,
                "hlc3": lambda: (self.high + self.low + self.close) / 3,
                "ohlc4": lambda: (self.open + self.high + self.low + self.close) / 4,
            }
            self.values[name] = derived[name]() if name in derived else getattr(self, name)
            return self.values[name]
        indicator = name.split(".", 1)[0]
        if indicator not in self.definitions:
            raise IndicatorError(f"Unknown series '{name}'")
        self._compute(indicator)
        if name not in self.values:
            fields = indicator_outputs(self.definitions[indicator]["type"])
            if fields:
                raise IndicatorError(
                    f"'{indicator}' has several outputs; refer to one of: "
                    + ", ".join(f"{indicator}.{field}" for field in fields)
                )
            raise IndicatorError(f"'{indicator}' has a single output; refer to it as '{indicator}'")
        return self.values[name]

    source = series

    def _compute(self, name):
        if name in self._computing:
            raise IndicatorError("Indicator sources form a loop: " + " -> ".join(self._computing + [name]))
        self._computing.append(name)
        try:
            definition = self.definitions[name]
            function, _, outputs, _, _ = INDICATORS[definition["type"]]
            result = function(self, resolve_parameters(name, definition, self.granularity_seconds))
            if outputs:
                for field in outputs:
                    self.values[f"{name}.{field}"] = result[field]
            else:
                self.values[name] = result
        finally:
            self._computing.pop()


def indicator_outputs(indicator_type):
    return INDICATORS[indicator_type][2]


def resolve_parameters(name, definition, granularity_seconds):
    """Validate a definition and fill in defaults, converting periods to candle counts."""
    indicator_type = definition.get("type")
    if indicator_type not in INDICATORS:
        raise IndicatorError(
            f"Indicator '{name}' has unknown type {indicator_type!r}; choose one of: {', '.join(INDICATORS)}"
        )
    _, parameters, _, _, _ = INDICATORS[indicator_type]
    allowed = set(parameters) | {"type", "plot", "label", "description"}
    unknown = sorted(set(definition) - allowed)
    if unknown:
        raise IndicatorError(
            f"Indicator '{name}' ({indicator_type}) does not take {', '.join(unknown)}; "
            f"its parameters are: {', '.join(parameters) or 'none'}"
        )
    resolved = {}
    for key, default in parameters.items():
        value = definition.get(key, default)
        if value is REQUIRED:
            raise IndicatorError(f"Indicator '{name}' ({indicator_type}) needs \"{key}\"")
        if key in PERIOD_PARAMETERS and value is not None:
            value = parse_period(value, granularity_seconds, f"'{name}' {key}")
        resolved[key] = value
    if indicator_type == "MACD" and resolved["fast"] >= resolved["slow"]:
        raise IndicatorError(f"Indicator '{name}' (MACD) needs fast shorter than slow")
    if indicator_type == "VWAP" and resolved["anchor"] not in (None, "day"):
        raise IndicatorError(f"Indicator '{name}' (VWAP) anchor must be \"day\" or omitted")
    return resolved


def output_names(definitions):
    """Every series name the definitions produce, e.g. "rsi" or "macd.signal"."""
    names = []
    for name, definition in definitions.items():
        outputs = indicator_outputs(definition["type"])
        names += [f"{name}.{field}" for field in outputs] if outputs else [name]
    return names


def price_scale_outputs(name, definition):
    """The outputs that share the price axis, for charting; empty for oscillators."""
    indicator_type = definition["type"]
    function, parameters, outputs, overlay, _ = INDICATORS[indicator_type]
    if not definition.get("plot", overlay):
        return []
    if not overlay:
        return []  # oscillators have their own scale, so they get their own panel
    if outputs:
        return [f"{name}.{field}" for field in PRICE_SCALE_FIELDS.get(indicator_type, outputs)]
    return [name]


def panel_outputs(name, definition):
    """The outputs drawn in an oscillator's own chart panel; empty for price-scale indicators."""
    _, _, outputs, overlay, _ = INDICATORS[definition["type"]]
    if overlay or not definition.get("plot", True):
        return []
    return [f"{name}.{field}" for field in outputs] if outputs else [name]

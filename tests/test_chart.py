from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import numpy as np
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from backtest import run_backtest
from chart import BUY_COLOR, SELL_COLOR, BacktestChart, ModelValueChart
from tests.test_backtest import _candle, _profile


def _chart_with_result():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    result = run_backtest([_candle(i * 900, p) for i, p in enumerate(prices)], _profile())
    figure = Figure(figsize=(8, 4), dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = BacktestChart(figure, canvas)
    chart.show(result)
    canvas.draw()
    return chart, result


def test_chart_plots_price_trend_lines_and_trade_points():
    chart, result = _chart_with_result()

    labels = [line.get_label() for line in chart.ax.get_lines() if not line.get_label().startswith("_")]
    assert labels == ["Close", "fast_ema (EMA 2)", "slow_ema (EMA 5)", "emergency_ema (EMA 1)", "Buy", "Sell"]
    buys, sells = chart.ax.get_lines()[4:6]
    assert (buys.get_marker(), buys.get_color()) == ("^", BUY_COLOR)
    assert (sells.get_marker(), sells.get_color()) == ("v", SELL_COLOR)
    # The triangles sit at the chart's bottom edge (axes coordinates), at the trade's time, not on the price.
    assert buys.get_transform() == chart.ax.get_xaxis_transform() and list(buys.get_ydata()) == [0.025]
    assert list(buys.get_xdata()) == [np.datetime64(result["trades"][0]["entry_time"], "s")]
    assert list(sells.get_ydata()) == [0.065]
    # A vertical line marks each buy and sell through the whole chart.
    buy_lines, sell_lines = (c for c in chart.ax.collections if c.get_transform() == chart.ax.get_xaxis_transform())
    assert len(buy_lines.get_segments()) == len(result["trades"]) == len(sell_lines.get_segments())
    assert [point[1] for point in buy_lines.get_segments()[0]] == [0, 1]
    assert chart.ax.get_yscale() == "log"


def test_chart_hover_shows_tooltip_for_nearest_candle():
    chart, result = _chart_with_result()
    index = 7
    x_pixel, y_pixel = chart.ax.transData.transform((chart.x_values[index], chart.closes[index]))
    event = SimpleNamespace(inaxes=chart.ax, xdata=chart.x_values[index] + 1e-6, x=x_pixel, y=y_pixel)

    chart._on_move(event)

    assert chart.tooltip.get_visible()
    assert "Close  102.00" in chart.tooltip.get_text()
    assert "fast_ema (EMA 2)  101.56" in chart.tooltip.get_text()

    chart._on_move(SimpleNamespace(inaxes=None, xdata=None, x=0, y=0))
    assert not chart.tooltip.get_visible()


def test_focus_trade_zooms_price_chart_to_the_trade():
    chart, result = _chart_with_result()
    trade = result["trades"][0]

    chart.focus_trade(trade)

    left, right = chart.ax.get_xlim()
    assert left < mdates.date2num(np.datetime64(trade["entry_time"], "s"))
    assert right > mdates.date2num(np.datetime64(trade["exit_time"], "s"))
    low, high = chart.ax.get_ylim()
    assert low < min(trade["entry_price"], trade["exit_price"]) < high


def test_floor_sets_the_lowest_value_shown_and_outlasts_a_redraw():
    chart, result = _chart_with_result()
    automatic_bottom = chart.ax.get_ylim()[0]
    assert automatic_bottom < 85  # the lowest close

    assert chart.set_floor(90) is True
    assert chart.ax.get_ylim()[0] == 90
    assert chart.ax.get_ylim()[1] > 105  # the top still fits the highest close

    chart.show(result)  # e.g. another model chosen in Showing model
    assert chart.ax.get_ylim()[0] == 90

    chart.set_floor(None)
    assert chart.ax.get_ylim()[0] == automatic_bottom


def test_floor_above_every_value_is_reported_and_ignored():
    chart, _ = _chart_with_result()
    automatic = chart.ax.get_ylim()

    assert chart.set_floor(10_000) is False
    assert chart.ax.get_ylim() == automatic


def test_floor_applies_to_a_trade_zoom_without_hiding_the_trade():
    chart, result = _chart_with_result()
    trade = result["trades"][0]
    lowest_fill = min(trade["entry_price"], trade["exit_price"])

    chart.set_floor(lowest_fill * 0.999)
    chart.focus_trade(trade)
    assert lowest_fill * 0.999 <= chart.ax.get_ylim()[0] < lowest_fill

    chart.set_floor(lowest_fill * 1.01)  # above the exit: the zoom must still show it
    chart.focus_trade(trade)
    assert chart.ax.get_ylim()[0] < lowest_fill


def test_model_value_chart_floor():
    _, result = _chart_with_result()
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.set_floor(5_000)  # set before anything is shown
    chart.show(result)

    assert chart.ax.get_ylim()[0] == 5_000


def test_model_value_chart_plots_value_against_buy_and_hold():
    _, result = _chart_with_result()
    figure = Figure(figsize=(8, 4), dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = ModelValueChart(figure, canvas)
    chart.show(result)
    canvas.draw()

    labels = [line.get_label() for line in chart.ax.get_lines() if not line.get_label().startswith("_")]
    assert labels == ["Starting capital (10,000)", "Buy and hold from 1970-01-01", "Model value"]
    assert chart.model_values[-1] == result["ending_equity"]
    assert chart.benchmark_values[-1] == 10_000 * (1 + result["buy_and_hold_return"])
    assert len(chart.axes) == 2
    assert chart.tax_reserve_ax.get_ylabel() == "Tax reserve (USD)"
    assert chart.tax_reserve_ax.get_lines()[0].get_label() == "Tax reserve (USD)"

    x_pixel, y_pixel = chart.ax.transData.transform((chart.x_values[3], chart.model_values[3]))
    chart._on_move(SimpleNamespace(inaxes=chart.ax, xdata=chart.x_values[3], x=x_pixel, y=y_pixel))
    assert "Model value  10,000.00 (+0.00%)" in chart.tooltip.get_text()
    assert "Tax reserve (USD)  0.00" in chart.tooltip.get_text()


def test_model_value_chart_shows_monthly_tax_reserve_on_its_own_panel():
    _, result = _chart_with_result()
    reserve = [0.0] * len(result["series"]["time"])
    reserve[7:9] = [50.0, 50.0]
    result["series"]["tax_reserve"] = reserve
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.show(result)

    assert list(chart.tax_reserve_ax.get_lines()[0].get_ydata()) == reserve
    assert chart.tax_reserve_ax.get_ylim()[0] == 0


def test_model_value_chart_labels_mixed_tax_reserve_currencies():
    _, result = _chart_with_result()
    other = dict(result)
    other["product_id"] = "ETH-USDC"
    other["model_name"] = "eth-model"
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.show([result, other])

    assert chart.tax_reserve_ax.get_ylabel() == "Tax reserve (mixed quote currencies)"
    labels = [line.get_label() for line in chart.tax_reserve_ax.get_lines()
              if not line.get_label().startswith("_")]
    assert labels == [
        "Model 1 (USD)", "eth-model (USDC)",
    ]


def test_buy_and_hold_line_starts_where_the_model_could_first_buy_and_is_marked():
    _, result = _chart_with_result()
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.show(result)

    times = result["series"]["time"]
    start = times.index(result["buy_and_hold_start"])
    assert start > 0  # the indicators need a few candles first
    closes = result["series"]["close"]
    assert list(chart.benchmark_values[:start + 1]) == [10_000] * (start + 1)
    assert chart.benchmark_values[-1] == pytest.approx(10_000 * closes[-1] / closes[start])
    assert chart.benchmark_values[-1] == pytest.approx(10_000 * (1 + result["buy_and_hold_return"]))
    assert chart.ax.get_title(loc="right").endswith("from 1970-01-01")
    start_marker = [line for line in chart.ax.get_lines() if line.get_markerfacecolor() == "#fcfcfb"]
    assert len(start_marker) == 1 and list(start_marker[0].get_ydata()) == [10_000]


def test_model_value_chart_labels_the_starting_and_ending_value():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    profile = _profile()
    profile["capital_allocation"]["starting_capital"] = 2_500
    result = run_backtest([_candle(i * 900, p) for i, p in enumerate(prices)], profile)
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.show(result)

    labels = [text.get_text() for text in chart.ax.texts]
    assert "Start 2,500" in labels
    assert f"End {result['ending_equity']:,.0f}" in labels
    assert chart.ax.get_title(loc="right").startswith("Started at 2,500.00")


def test_model_value_chart_compares_several_models():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    results = []
    for name, slow in (("quick", 3), ("steady", 5)):
        profile = _profile()
        profile["indicators"]["slow_ema"]["period"] = slow
        result = run_backtest(candles, profile)
        result["model_name"] = name
        results.append(result)
    figure = Figure(figsize=(8, 4), dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = ModelValueChart(figure, canvas)
    chart.show(results, primary=1)
    canvas.draw()

    labels = [line.get_label() for line in chart.ax.get_lines() if not line.get_label().startswith("_")]
    assert labels[:2] == ["Starting capital (10,000)", "Buy and hold from 1970-01-01 (steady's start)"]
    assert labels[2].startswith("quick: ") and labels[3].startswith("steady: ")
    assert chart.model_values is chart.run_values[1]  # the hover follows the primary model
    assert "2 models" in chart.ax.get_title(loc="left")
    assert chart.ax.get_legend()._loc == 4  # lower right, clear of the model lines

    x_pixel, y_pixel = chart.ax.transData.transform((chart.x_values[3], chart.model_values[3]))
    chart._on_move(SimpleNamespace(inaxes=chart.ax, xdata=chart.x_values[3], x=x_pixel, y=y_pixel))
    text = chart.tooltip.get_text()
    assert "quick  10,000.00" in text and "steady  10,000.00" in text and "Buy and hold" in text


def _chart_with_indicators(indicators, size=(10, 8)):
    from tests.test_indicators import _candles

    profile = _profile(indicators=indicators)
    profile["strategy"]["entry_signal"] = {"all": ["close > 0"]}
    profile["strategy"]["exit_signal"] = {"any": ["close < 0"]}
    result = run_backtest(_candles(300), profile)
    figure = Figure(figsize=size, dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = BacktestChart(figure, canvas)
    chart.show(result)
    canvas.draw()
    return chart, result


def test_each_oscillator_gets_its_own_panel_sharing_the_time_axis():
    chart, _ = _chart_with_indicators({
        "trend": {"type": "EMA", "period": 20},
        "rsi": {"type": "RSI", "period": 14},
        "macd": {"type": "MACD"},
        "atr": {"type": "ATR", "period": 14},
    })

    assert len(chart.axes) == 4  # price, then RSI, MACD and ATR panels
    assert [meta["name"] for meta in chart.panels] == ["rsi", "macd", "atr"]
    rsi_ax, macd_ax, _ = chart.axes[1:]
    assert [line.get_label() for line in macd_ax.get_lines() if not line.get_label().startswith("_")] == [
        "line", "signal", "histogram"]
    assert rsi_ax.get_ylim() == (0, 100)
    reference_levels = {line.get_ydata()[0] for line in rsi_ax.get_lines() if len(line.get_ydata()) == 2}
    assert {30, 70} <= reference_levels
    assert all(ax.get_shared_x_axes().joined(ax, chart.ax) for ax in chart.axes[1:])


def test_every_price_scale_indicator_is_drawn_and_repeats_are_dashed():
    chart, _ = _chart_with_indicators({f"ma_{n}": {"type": "SMA", "period": n} for n in range(2, 9)})

    lines = [line for line in chart.ax.get_lines() if line.get_label().startswith("ma_")]
    assert len(lines) == 7
    assert [line.get_linestyle() for line in lines] == ["-"] * 5 + ["--"] * 2
    assert lines[5].get_color() == lines[0].get_color()
    assert len(chart.axes) == 1  # no oscillators, so no panels


def test_plot_false_hides_an_indicator_and_hovering_a_panel_moves_every_crosshair():
    chart, _ = _chart_with_indicators({
        "rsi": {"type": "RSI", "period": 14},
        "quiet": {"type": "ATR", "period": 14, "plot": False},
    })
    assert len(chart.axes) == 2

    panel = chart.axes[1]
    index = 200
    x_pixel, y_pixel = panel.transData.transform((chart.x_values[index], 50))
    chart._on_move(SimpleNamespace(inaxes=panel, xdata=chart.x_values[index], x=x_pixel, y=y_pixel))

    assert all(crosshair.get_visible() for crosshair in chart.crosshairs)
    assert {crosshair.get_xdata()[0] for crosshair in chart.crosshairs} == {chart.x_values[index]}
    assert "quiet (ATR 14)" in chart.tooltip.get_text()  # off the chart, still in the tooltip
    assert not chart.tooltip.get_in_layout()


def test_the_price_axis_is_scaled_to_the_price_not_to_lines_far_off_its_scale():
    # An SMA of OBV sits on the price chart but runs to millions: it must not squash the price.
    prices = [0.1 + 0.002 * i for i in range(40)]
    candles = [{**_candle(i * 900, p), "volume": 1e9} for i, p in enumerate(prices)]
    profile = _profile(indicators={"fast_ema": {"type": "EMA", "period": 2}, "slow_ema": {"type": "EMA", "period": 5},
                                   "emergency_ema": {"type": "EMA", "period": 1},
                                   "obv": {"type": "OBV"}, "obv_sma": {"type": "SMA", "period": 3, "source": "obv"}})
    result = run_backtest(candles, profile)
    assert max(v for v in result["series"]["indicators"]["obv_sma"] if v == v) > 1e9
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = BacktestChart(figure, FigureCanvasAgg(figure))

    chart.show(result)

    low, high = chart.ax.get_ylim()
    assert max(prices) < high < max(prices) * 1.1
    assert low < min(prices)  # the lowest close, and the trade strip below it


def test_indicator_lines_are_drawn_at_30_percent_opacity_and_the_price_is_not():
    chart, _ = _chart_with_result()

    close = next(line for line in chart.ax.get_lines() if line.get_label() == "Close")
    assert close.get_alpha() is None
    lines = [line for lines in chart.indicator_lines.values() for line in lines]
    assert lines and all(line.get_alpha() == pytest.approx(0.3) for line in lines)


def test_indicators_switch_off_together_or_one_at_a_time_and_stay_so_for_the_next_model():
    chart, result = _chart_with_result()
    label = "fast_ema (EMA 2)"

    chart.set_indicators_shown(False)
    assert not any(line.get_visible() for lines in chart.indicator_lines.values() for line in lines)
    chart.set_indicators_shown(True)
    chart.toggle_indicator(label)
    assert not any(line.get_visible() for line in chart.indicator_lines[label])
    assert all(line.get_visible() for line in chart.indicator_lines["slow_ema (EMA 5)"])

    chart.show(result)  # another model, or a redraw
    assert not any(line.get_visible() for line in chart.indicator_lines[label])
    entry = next(artist for artist, name in chart._legend_entries.items() if name == label)
    assert entry.get_alpha() == pytest.approx(0.3)

    changed = []
    chart.on_indicators_changed = lambda: changed.append(chart.indicators_shown)
    chart._on_pick(SimpleNamespace(artist=entry))  # clicking the legend entry shows it again
    assert all(line.get_visible() for line in chart.indicator_lines[label]) and changed == [True]
    close = next(line for line in chart.ax.get_lines() if line.get_label() == "Close")
    assert close.get_visible()


def test_hover_values_go_beside_the_chart_instead_of_over_it():
    chart, result = _chart_with_result()
    shown = []
    chart.on_hover = shown.append
    index = 7
    x_pixel, y_pixel = chart.ax.transData.transform((chart.x_values[index], chart.closes[index]))

    chart._on_move(SimpleNamespace(inaxes=chart.ax, xdata=chart.x_values[index] + 1e-6, x=x_pixel, y=y_pixel))

    assert "Close  102.00" in shown[-1] and "fast_ema (EMA 2)  101.56" in shown[-1]
    assert not chart.tooltip.get_visible()  # no tooltip over the chart
    assert all(crosshair.get_visible() for crosshair in chart.crosshairs) and chart.hover_dot.get_visible()

    chart._on_move(SimpleNamespace(inaxes=None, xdata=None, x=0, y=0))
    assert shown[-1] is None


def test_a_semi_live_session_carries_the_indicator_lines_on(tmp_path):
    import tests.test_semi_live as t
    history = t._history()
    session, models = t._session(tmp_path, history)
    session.add_tick(t.STARTED + 600, history[-1]["close"] * 1.02)
    result = run_backtest(history, models[0][1])
    result["model_name"] = "ema"
    figure = Figure(figsize=(10, 6), dpi=100)
    chart = BacktestChart(figure, FigureCanvasAgg(figure))
    chart.show(result)
    label = "fast_ema (EMA 2)"
    before = len(chart.indicator_lines[label])

    chart.show_live(session.snapshot(t.STARTED + 600))

    lines = chart.indicator_lines[label]
    assert len(lines) == before + 1
    extension = lines[-1]
    assert extension.get_color() == lines[0].get_color() and extension.get_alpha() == lines[0].get_alpha()
    assert mdates.num2date(extension.get_xdata()[-1]).timestamp() == pytest.approx(t.STARTED + 600)
    chart.toggle_indicator(label)  # the live part hides with its indicator
    assert not extension.get_visible()

    chart.show_live(session.snapshot(t.STARTED + 900))  # an update replaces the live part, keeping one
    assert len(chart.indicator_lines[label]) == before + 1 and extension not in chart.indicator_lines[label]
    assert not chart.indicator_lines[label][-1].get_visible()

from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import numpy as np
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
    assert list(buys.get_ydata()) == [result["trades"][0]["entry_price"]]
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


def test_model_value_chart_plots_value_against_buy_and_hold():
    _, result = _chart_with_result()
    figure = Figure(figsize=(8, 4), dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = ModelValueChart(figure, canvas)
    chart.show(result)
    canvas.draw()

    labels = [line.get_label() for line in chart.ax.get_lines() if not line.get_label().startswith("_")]
    assert labels == ["Starting capital (10,000)", "Buy and hold", "Model value"]
    assert chart.model_values[-1] == result["ending_equity"]
    assert chart.benchmark_values[-1] == 10_000 * (1 + result["buy_and_hold_return"])

    x_pixel, y_pixel = chart.ax.transData.transform((chart.x_values[3], chart.model_values[3]))
    chart._on_move(SimpleNamespace(inaxes=chart.ax, xdata=chart.x_values[3], x=x_pixel, y=y_pixel))
    assert "Model value  10,000.00 (+0.00%)" in chart.tooltip.get_text()


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
    assert labels[:2] == ["Starting capital (10,000)", "Buy and hold"]
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

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from backtest import run_backtest
from chart import BacktestChart, ModelValueChart, extent_in, fit_axis
from tests.test_backtest import _candle, _profile
from time_window import describe_days, fractions, moved, window_for, zoomed

DAY = 86_400


def test_a_window_ends_at_the_data_end_by_default_and_stays_inside_it():
    assert window_for(0, 1000, 365) == (635, 1000)
    assert window_for(0, 1000, 365, end=500) == (135, 500)
    assert window_for(0, 1000, 365, end=100) == (0, 365)  # can't start before the data
    assert window_for(0, 1000, None) == (0, 1000)
    assert window_for(0, 100, 365) == (0, 100)  # longer than the data: all of it


def test_moving_and_the_scroll_bar_fractions():
    assert moved(0, 1000, 600, 700, 950) == (900, 1000)
    assert moved(0, 1000, 600, 700, -50) == (0, 100)
    assert fractions(0, 1000, 250, 500) == (0.25, 0.5)
    assert fractions(5, 5, 5, 5) == (0.0, 1.0)


def test_zooming_keeps_the_point_under_the_pointer():
    start, days = zoomed(0, 1000, 400, 600, 0.5, anchor=0.5)
    assert (start, days) == (450, 100)
    start, days = zoomed(0, 1000, 400, 600, 0.5, anchor=0.0)
    assert start == 400 and days == 100
    assert zoomed(0, 1000, 0, 900, 2.0)[1] is None  # more than everything: all of it
    assert describe_days(365) == "1 year" and describe_days(45) == "45 days" and describe_days(0.25) == "6 hours"


def test_fit_axis_uses_only_the_values_in_the_window():
    figure = Figure()
    ax = figure.add_subplot()
    x = [float(i) for i in range(10)]
    series = [(np.array(x), np.array([1, 2, 3, 4, 5, 100, 7, 8, 9, 10.0]))]

    assert extent_in(series, 0, 4) == (1, 5)
    assert fit_axis(ax, series, 0, 4)
    low, high = ax.get_ylim()
    assert 0 <= low < 1 and 5 < high < 6
    assert not fit_axis(ax, series, 20, 30)  # nothing in view: left as it was
    fit_axis(ax, series, 0, 4, log=True)
    assert 0.9 < ax.get_ylim()[0] < 1 and 5 < ax.get_ylim()[1] < 5.5


def _long_result():
    # Two years of daily candles: the price climbs to 50, then falls back to 10 in the second year.
    prices = [10 + 40 * i / 365 for i in range(365)] + [50 - 40 * i / 365 for i in range(365)]
    return run_backtest([_candle(i * DAY, p) for i, p in enumerate(prices)], _profile(period_granularity="ONE_DAY"))


def test_a_window_of_the_price_chart_fits_the_price_in_it():
    import matplotlib.dates as mdates
    result = _long_result()
    figure = Figure(figsize=(12, 8))
    chart = BacktestChart(figure, FigureCanvasAgg(figure))
    chart.show(result)
    first, last = chart.view_range()
    assert last - first == pytest.approx(729)

    chart.show_window(first, first + 100)  # the first 100 days: 10 up to about 21

    assert chart.ax.get_xlim() == pytest.approx((first, first + 100))
    low, high = chart.ax.get_ylim()
    assert 21 < high < 23 and low < 10
    assert chart.axes[0].get_xlim() == chart.ax.get_xlim()
    assert mdates.num2date(first).year == 1970


def test_the_value_chart_window_spans_every_model():
    result = _long_result()
    later = run_backtest([_candle(DAY * (100 + i), 10.0 + i % 3) for i in range(800)],
                         _profile(period_granularity="ONE_DAY"))
    figure = Figure()
    chart = ModelValueChart(figure, FigureCanvasAgg(figure))
    chart.show([result, later])

    first, last = chart.view_range()

    assert last - first == pytest.approx(899)
    chart.show_window(last - 30, last)
    assert chart.ax.get_xlim() == pytest.approx((last - 30, last))

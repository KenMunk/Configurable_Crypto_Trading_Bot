import numpy as np
import pytest

from backtest import run_backtest
from model_config import CompiledModel, ProfileError


def _candle(start, close, low=None, high=None, open_=None):
    return {"start": start, "open": close if open_ is None else open_, "high": close if high is None else high,
            "low": close if low is None else low, "close": close, "volume": 1.0}


def _candles(prices):
    return [_candle(i * 900, p) for i, p in enumerate(prices)]


def _profile(regimes, entry="close > 1000", exit_="close > 99", safeguards=None):
    return {
        "product_id": "BTC-USD",
        "indicators": {"ema": {"type": "EMA", "period": 2}},
        "strategy": {"direction": "LONG_ONLY", "entry_signal": {"all": [entry]}, "exit_signal": {"any": [exit_]}},
        "friction_model": {"exchange_commission_rate": 0.0, "slippage_buffer": 0.0},
        "capital_allocation": {"target_position_fraction": 1.0},
        "safeguards": safeguards or {"use_hard_stop": False},
        "regimes": regimes,
    }


def _active(profile, prices):
    model = CompiledModel(profile, 900)
    series, _, _ = model.evaluate(_candles(prices))
    return model.regimes.active(series, len(prices)).tolist()


def test_a_profile_without_regimes_compiles_none():
    profile = _profile(None)
    del profile["regimes"]

    assert CompiledModel(profile, 900).regimes is None


def test_a_regime_activates_only_after_its_condition_has_persisted_for_confirm():
    profile = _profile({"confirm": 3, "list": [{"name": "high", "when": "close > 110"}]})

    active = _active(profile, [100] * 5 + [120] * 5 + [100] * 5)

    # The condition starts on candle 5; three candles of it confirm it on candle 7. Back to base takes 3 too.
    assert active == [0] * 7 + [1] * 5 + [0] * 3


def test_an_active_regime_holds_for_min_hold_before_switching_again():
    profile = _profile({"min_hold": 4, "list": [{"name": "high", "when": "close > 110"}]})

    active = _active(profile, [100, 120, 100, 120, 100, 100, 100])

    assert active == [0, 1, 1, 1, 1, 0, 0]


def test_the_first_listed_matching_regime_wins():
    profile = _profile({"list": [{"name": "very_high", "when": "close > 130"}, {"name": "high", "when": "close > 110"}]})

    assert _active(profile, [100, 120, 140, 120]) == [0, 2, 1, 2]


def _dip_profile(on_switch, safeguards=None):
    return _profile({"on_switch": on_switch, "list": [
        {"name": "dip", "when": "close < 90", "entry_signal": {"all": ["close < 90"]},
         "exit_signal": {"any": ["close > 1000"]}, "safeguards": safeguards or {}}]})


def test_each_candle_trades_on_its_active_regimes_signals_and_keeps_the_position_on_a_switch():
    # Base never enters; the dip regime buys below 90. Back in base, base's exit sells above 99.
    result = run_backtest(_candles([100, 100, 85, 85, 96, 100]), _dip_profile("keep"))

    trade = result["trades"][0]
    assert (trade["entry_price"], trade["exit_price"], trade["exit_reason"]) == (85, 100, "EXIT_SIGNAL")
    assert [s["to"] for s in result["regimes"]["switches"]] == ["dip", "base"]
    assert result["regimes"]["share"] == {"base": pytest.approx(4 / 6), "dip": pytest.approx(2 / 6)}


def test_on_switch_close_sells_the_position_when_the_regime_changes():
    result = run_backtest(_candles([100, 100, 85, 85, 96, 100]), _dip_profile("close"))

    trade = result["trades"][0]
    assert (trade["exit_price"], trade["exit_reason"]) == (96, "REGIME_SWITCH")


def test_a_regime_uses_its_own_stop():
    candles = _candles([100, 100, 85])
    candles.append(_candle(3 * 900, 86, low=80, open_=85))

    result = run_backtest(candles, _dip_profile("keep", {"use_hard_stop": True, "hard_stop_percentage": 0.05}))

    trade = result["trades"][0]
    assert trade["exit_reason"] == "HARD_STOP"
    assert trade["exit_price"] == pytest.approx(85 * 0.95)


def test_regime_problems_are_all_reported():
    profile = _profile({"on_switch": "sometimes", "confirm": "1m", "list": [
        {"name": "a", "colour": "red"}, {"name": "a", "when": "nonsense > 1"}]})

    with pytest.raises(ProfileError) as caught:
        CompiledModel(profile, 900)

    text = "\n".join(caught.value.errors)
    for expected in ('on_switch must be "keep" or "close"', "shorter than one candle", "unknown keys: colour",
                     'needs a "when" rule', "names must be unique", "nonsense"):
        assert expected in text


def test_no_regime_enters_before_every_detector_has_warmed_up():
    # The dip regime could buy at once, but the slow detector needs 6 candles: nothing enters before then.
    profile = _profile({"list": [
        {"name": "slow", "when": "close > slow_ema * 2"},
        {"name": "dip", "when": "close < 90", "entry_signal": {"all": ["close < 90"]},
         "exit_signal": {"any": ["close > 1000"]}}]})
    profile["indicators"]["slow_ema"] = {"type": "EMA", "period": 6}

    result = run_backtest(_candles([85, 85, 85, 85, 85, 85, 85, 85]), profile)

    assert result["trades"] == [] and result["open_position"]
    assert result["ready_to_trade"] == 5 * 900
    assert result["buy_and_hold_start"] == 5 * 900


# --- reports and charts ---------------------------------------------------------------------------------------

def test_a_result_carries_each_candles_regime_and_a_summary_of_every_regime():
    result = run_backtest(_candles([100, 100, 85, 85, 96, 100]), _dip_profile("keep"))

    assert result["series"]["regime"] == [0, 0, 1, 1, 0, 0]
    regimes = result["regimes"]
    assert regimes["names"] == ["base", "dip"]
    assert (regimes["confirm_candles"], regimes["min_hold_candles"], regimes["on_switch"]) == (1, 1, "keep")
    dip = regimes["summary"][1]
    assert (dip["when"], dip["entry_all"], dip["exit_any"]) == ("close < 90", ["close < 90"], ["close > 1000"])
    assert regimes["summary"][0]["when"] is None


def test_a_result_without_regimes_has_no_regime_series():
    profile = _profile(None)
    del profile["regimes"]

    result = run_backtest(_candles([100, 101, 102]), profile)

    assert "regime" not in result["series"] and result["regimes"] is None


def _render(chart_class, result):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    figure = Figure(figsize=(8, 4), dpi=100)
    canvas = FigureCanvasAgg(figure)
    chart = chart_class(figure, canvas)
    chart.show(result)
    canvas.draw()
    return chart


@pytest.mark.parametrize("chart_name", ["BacktestChart", "ModelValueChart"])
def test_charts_mark_each_regime_and_every_switch(chart_name):
    import chart as charts
    result = run_backtest(_candles([100, 100, 85, 85, 96, 100]), _dip_profile("keep"))

    chart = _render(getattr(charts, chart_name), result)

    # A strip along the top in each regime's color: base, dip, base.
    strips = [patch for patch in chart.ax.patches if patch.get_y() == charts.REGIME_STRIP[0]]
    assert [patch.get_facecolor()[:3] for patch in strips] == [
        matplotlib_rgb(charts.regime_color(n)) for n in (0, 1, 0)]
    legend = [text.get_text() for text in chart.ax.get_legend().get_texts()]
    assert "Regime: base" in legend and "Regime: dip" in legend
    # A dashed line on every panel where the regime switches: into dip, and back to base.
    switch_times = sorted({line.get_xdata()[0] for ax in chart.axes for line in ax.get_lines()
                           if line.get_linestyle() == "--" and line.get_alpha() == 0.8})
    assert switch_times == [np.datetime64(2 * 900, "s"), np.datetime64(4 * 900, "s")]
    assert "Regime  dip" in chart.tooltip_text(2) and "Regime  base" in chart.tooltip_text(5)


def test_no_regime_is_marked_while_the_detectors_warm_up():
    import chart as charts
    profile = _dip_profile("keep")
    profile["indicators"]["slow_ema"] = {"type": "EMA", "period": 4}
    profile["regimes"]["list"].append({"name": "slow", "when": "close > slow_ema * 2"})

    result = run_backtest(_candles([85, 85, 85, 85, 85, 85]), profile)

    assert result["regimes"]["switches"][0]["time"] == 3 * 900  # dip only once the slow detector is ready
    assert charts.regime_runs(result) == [(1, 3, 5)]
    assert charts.regime_at(result, 1) == "warming up"


def test_simulator_text_describes_the_regimes_and_where_the_run_switched():
    from simulator import describe_profile, format_backtest_result
    profile = _dip_profile("close", {"use_hard_stop": True, "hard_stop_percentage": 0.05})

    valid, report = describe_profile(profile)
    text = format_backtest_result(run_backtest(_candles([100, 100, 85, 85, 96, 100]), profile))

    assert valid
    for lines in (report, text):
        assert "Regimes (1 plus base)" in lines and "an open position is sold" in lines
        assert "- dip (when close < 90): hard stop 5%, position 100% of cash" in lines
        assert "    buy: all of: close < 90" in lines
    assert "Time in each regime: base 67%, dip 33%" in text
    assert "Regime switches: 2" in text and "base -> dip" in text and "dip -> base" in text


def matplotlib_rgb(color):
    from matplotlib.colors import to_rgb
    return to_rgb(color)

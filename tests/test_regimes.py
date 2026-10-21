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

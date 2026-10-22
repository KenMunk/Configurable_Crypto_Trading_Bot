"""The drawdown limit's cooldown, and turning flash-crash buys off in synthetic and stress runs."""

import pytest

from backtest import run_backtest
from tests.test_backtest import _always_in as _always_in_15m, _candle


def _always_in(**safeguards):
    """A profile that buys at once and never sells on a signal, on daily candles so "3d" is three candles."""
    return {**_always_in_15m(**safeguards), "period_granularity": "ONE_DAY"}


def _prices(*prices):
    return [_candle(i * 86_400, price) for i, price in enumerate(prices)]


def _pauses(result):
    return [(p["paused_at"] // 86_400, None if p["resumed_at"] is None else p["resumed_at"] // 86_400)
            for p in result["drawdown_pauses"]]


def test_without_a_cooldown_a_long_window_keeps_the_model_out_for_good():
    candles = _prices(100, 100, 80, 80, 90, 100, 110)  # falls 20%, then recovers past where it started
    result = run_backtest(candles, _always_in(max_drawdown_limit=0.1, drawdown_window="1000d"))

    assert _pauses(result) == [(2, None)] and result["trade_count"] == 1  # never room under the old peak again


def test_with_a_cooldown_a_windowed_limit_resumes_and_measures_afresh():
    candles = _prices(100, 100, 80, 80, 90, 100, 110)
    result = run_backtest(candles, _always_in(max_drawdown_limit=0.1, drawdown_window="1000d",
                                              drawdown_cooldown="3d"))

    assert _pauses(result) == [(2, 5)]  # stopped on day 2, trading again three days later
    assert result["trades"][0]["exit_reason"] == "DRAWDOWN_STOP" and result["open_position"]
    assert result["ending_equity"] > run_backtest(candles, _always_in(
        max_drawdown_limit=0.1, drawdown_window="1000d"))["ending_equity"]  # it caught the recovery


def test_with_a_cooldown_a_plain_limit_resumes_although_the_signal_never_switched_off():
    candles = _prices(100, 100, 80, 85, 90, 95, 100)
    with_cooldown = run_backtest(candles, _always_in(max_drawdown_limit=0.1, drawdown_cooldown="2d"))
    without = run_backtest(candles, _always_in(max_drawdown_limit=0.1))

    assert _pauses(with_cooldown) == [(2, 4)] and with_cooldown["open_position"]
    assert _pauses(without) == [(2, None)]  # "close > 0" never switches off, so no fresh signal ever comes


def test_a_cooldown_is_not_cut_short_by_a_fresh_signal():
    profile = _always_in(max_drawdown_limit=0.1, drawdown_cooldown="4d")
    profile["strategy"]["entry_signal"] = {"all": ["close != 80"]}  # off on day 2, on again on day 3
    result = run_backtest(_prices(100, 100, 80, 90, 95, 100, 105, 110), profile)

    assert _pauses(result) == [(2, 6)]


def test_a_bad_cooldown_is_reported():
    with pytest.raises(Exception, match="drawdown_cooldown"):
        run_backtest(_prices(100, 101), _always_in(max_drawdown_limit=0.1, drawdown_cooldown="soon"))


# --- flash-crash buys in synthetic and stress runs --------------------------------------------------------

def test_without_flash_crash_buys_drops_only_the_ladder():
    from stress_test import without_flash_crash_buys
    profile = _always_in()
    profile["strategy"]["flash_crash_buy"] = {"levels": [{"drop": 0.3, "allocation": 1.0}]}

    stripped = without_flash_crash_buys(profile)

    assert "flash_crash_buy" not in stripped["strategy"] and "flash_crash_buy" in profile["strategy"]
    assert stripped["strategy"]["entry_signal"] == profile["strategy"]["entry_signal"]


def test_a_stress_test_without_flash_crash_buys_runs_each_model_as_if_it_had_no_ladder():
    import stress_test
    from friction import FrictionModel
    from tests.test_stress_test import SCENARIOS, _daily, _profile
    plain = _profile(period_granularity="ONE_DAY")
    ladder = {**plain, "strategy": {**plain["strategy"],
                                    "flash_crash_buy": {"levels": [{"drop": 0.05, "allocation": 1.0}]}}}
    combos = stress_test.combinations(SCENARIOS, [], pairs=1, max_paths=1)
    markets = {("BTC-USD", "ONE_DAY"): _daily()}
    returns = lambda records: [(r["scenario"], r["mode"], r["return"]) for r in records]  # noqa: E731

    off, _, _ = stress_test.run_stress([("m", ladder)], {"m": FrictionModel(ladder)}, markets, combos,
                                       flash_crash_buys=False)
    none, _, _ = stress_test.run_stress([("m", plain)], {"m": FrictionModel(plain)}, markets, combos)

    assert returns(off) == returns(none)


def test_flash_crash_buys_are_off_by_default_in_synthetic_runs():
    from scenario_editor import DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["synthetic_flash_crash_buys"] is False

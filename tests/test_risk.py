import pytest

from backtest import flash_crash_levels_of, run_backtest
from friction import FrictionModel
from tests.test_backtest import _candle, _profile

STEP = 900


def _limited(profile=None, limit=0.10, window="180d"):
    profile = profile or _profile()
    profile["safeguards"] = {"use_hard_stop": False, "max_drawdown_limit": limit, "drawdown_window": window}
    return profile


def test_drawdown_stop_sells_exactly_at_the_limit_below_the_window_high():
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 105, 110])]
    candles.append(_candle(9 * STEP, 108, low=95, open_=110))  # dips through the floor and recovers

    result = run_backtest(candles, _limited())

    stop = result["trades"][0]
    assert stop["exit_reason"] == "DRAWDOWN_STOP"
    units = 10_000 / 101
    assert stop["exit_price"] == pytest.approx(0.9 * units * 110 / units)  # 10% below the 110 peak value
    assert result["max_window_drawdown"] == pytest.approx(0.10)
    assert result["drawdown_pauses"][0]["resumed_at"] is None  # waits for a fresh signal
    assert result["trade_count"] == 1


def test_drawdown_window_forgets_old_highs():
    # A 20% slide spread over more than the window never breaches a 10% limit measured over 1 hour.
    prices = [100] * 6 + [101, 105, 110] + [110 * 0.98 ** k for k in range(1, 12)]
    candles = [_candle(i * STEP, p) for i, p in enumerate(prices)]
    profile = _limited(window="1h")
    profile["strategy"]["exit_signal"]["any"] = ["close < 0"]  # hold through the slide

    result = run_backtest(candles, profile)

    assert all(trade["exit_reason"] != "DRAWDOWN_STOP" for trade in result["trades"])
    assert result["max_window_drawdown"] <= 0.10 + 1e-9
    assert result["max_drawdown"] > 0.15  # the all-time drawdown is still reported in full


def test_no_entry_without_room_above_the_floor():
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 102, 103, 104])]
    profile = _limited(limit=0.03)
    profile["friction_model"] = {"exchange_commission_rate": 0.01, "slippage_buffer": 0.0}
    # Two round trips at 1% per fill need 4% of room; a 3% limit never leaves enough.

    result = run_backtest(candles, profile)

    assert result["trade_count"] == 0


def test_tax_payments_are_withdrawals_not_drawdowns():
    year = 365 * 86_400
    prices = [100] * 6 + [101, 102, 103, 104, 105, 110] + [110] * 10
    candles = [_candle(i * STEP, p) for i, p in enumerate(prices)]
    candles.append(_candle(year + 3 * 86_400, 110))  # next year: taxes fall due, prices flat
    profile = _limited()
    profile["friction_model"] = {"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3}
    profile["strategy"]["exit_signal"]["profit_lock_target"] = 0.05
    profile["execution"] = {"max_orders_per_day": 2}

    result = run_backtest(candles, profile)

    assert result["taxes_paid"] > 0
    assert result["series"]["equity"][-1] < result["series"]["equity"][-2]  # the payment reduced the value
    assert result["max_window_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert result["max_drawdown"] == pytest.approx(0.0, abs=1e-9)


def _flash_profile(levels):
    profile = _profile()
    profile["strategy"]["entry_signal"] = {"all": ["close > 1000"]}  # the main strategy never trades
    profile["strategy"]["flash_crash_buy"] = {"enabled": True, "levels": levels}
    return profile


def test_flash_crash_limit_buys_fill_at_their_price_and_sell_at_the_close():
    candles = [_candle(i * STEP, 100) for i in range(3)]
    candles.append(_candle(3 * STEP, 98, low=40, open_=100))
    levels = [{"drop": 0.3, "allocation": 0.1}, {"drop": 0.5, "allocation": 0.1}, {"drop": 0.9, "allocation": 0.1}]
    profile = _flash_profile(levels)
    friction = FrictionModel(profile, {"exchange_fees": {"maker": 0.005, "taker": 0.009, "tier": "Test"}})

    result = run_backtest(candles, profile, friction=friction)

    fills = result["flash_crash_buys"]
    assert [fill["entry_price"] for fill in fills] == pytest.approx([70, 50])  # the 90% level was never reached
    first = fills[0]
    assert first["position_cost"] == pytest.approx(1_000)
    bought = (1_000 - 1_000 * 0.005) / 70  # maker fee on the resting buy
    assert first["pnl"] == pytest.approx(bought * 98 * (1 - 0.009) - 1_000)  # taker fee on the sale
    assert first["exit_reason"] == "FLASH_REBOUND_SELL" and first["entry_reason"] == "FLASH_CRASH_BUY_30%"
    assert result["ending_equity"] == pytest.approx(10_000 + sum(fill["pnl"] for fill in fills))
    assert result["first_entry"] is None  # flash fills are not the strategy's entries


def test_flash_crash_levels_are_validated():
    assert flash_crash_levels_of(_profile()) == []
    assert flash_crash_levels_of(_flash_profile([{"drop": 0.9, "allocation": 0.1},
                                                 {"drop": 0.5, "allocation": 0.2}])) == [(0.5, 0.2), (0.9, 0.1)]
    for bad in ([], [{"drop": 1.5, "allocation": 0.1}], [{"drop": 0.5, "allocation": 0}], ["0.5"]):
        with pytest.raises(ValueError, match="flash_crash_buy"):
            flash_crash_levels_of(_flash_profile(bad))


def test_flash_crash_reserve_sizes_orders_from_a_fixed_amount_the_strategy_never_trades():
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 102])]
    candles.append(_candle(8 * STEP, 101, low=5, open_=102))
    candles += [_candle(9 * STEP, 90), _candle(10 * STEP, 80)]  # the trend ends, closing the strategy's trade
    profile = _profile()
    profile["capital_allocation"]["target_position_fraction"] = 1.0
    profile["strategy"]["flash_crash_buy"] = {"enabled": True, "reserve": 100,
                                              "levels": [{"drop": 0.5, "allocation": 0.25},
                                                         {"drop": 0.9, "allocation": 0.75}]}

    result = run_backtest(candles, profile)

    strategy_trade = next(t for t in result["trades"] if t["entry_reason"] == "ENTRY_SIGNAL")
    assert strategy_trade["position_cost"] == pytest.approx(10_000 - 100)  # the reserve stays set aside
    fills = result["flash_crash_buys"]
    assert [fill["position_cost"] for fill in fills] == pytest.approx([25, 75])
    overcommitted = _flash_profile([{"drop": 0.5, "allocation": 0.6}, {"drop": 0.9, "allocation": 0.6}])
    overcommitted["strategy"]["flash_crash_buy"]["reserve"] = 100
    with pytest.raises(ValueError, match="add up to at most 1"):
        flash_crash_levels_of(overcommitted)


def test_trailing_stop_sells_below_the_highest_price_since_entry():
    prices = [100] * 6 + [101, 110, 120]
    candles = [_candle(i * STEP, p) for i, p in enumerate(prices)]
    candles.append(_candle(len(prices) * STEP, 119, low=105, open_=120))
    profile = _profile()
    profile["strategy"]["exit_signal"]["trailing_stop"] = 0.1

    result = run_backtest(candles, profile)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["exit_price"] == pytest.approx(120 * 0.9)  # 10% under the 120 high


def _holding_with_reserve(crash_candle):
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 105])]
    candles.append(crash_candle)
    profile = _limited()
    profile["strategy"]["exit_signal"]["any"] = ["close < 0"]  # only stops can exit
    profile["strategy"]["flash_crash_buy"] = {"enabled": True, "reserve": 100,
                                              "levels": [{"drop": 0.3, "allocation": 0.5},
                                                         {"drop": 0.99, "allocation": 0.5}]}
    return run_backtest(candles, profile)


def test_a_flash_crash_is_bought_with_the_reserve_and_never_sold_into():
    result = _holding_with_reserve(_candle(8 * STEP, 104, low=0.06, open_=0.06))  # an April-2017-style print

    assert [t["exit_reason"] for t in result["trades"]] == ["FLASH_REBOUND_SELL", "FLASH_REBOUND_SELL"]
    assert result["open_position"] is True  # the strategy's position was held through the print
    assert result["max_window_drawdown"] < 0.10


def _holding_through(crash_closes, recovery_close=None):
    """Hold a position into a crash whose candles close at `crash_closes`, then optionally recover."""
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 105])]
    for close in crash_closes:
        candles.append(_candle(len(candles) * STEP, close, low=close * 0.9, open_=candles[-1]["close"]))
    if recovery_close is not None:
        candles.append(_candle(len(candles) * STEP, recovery_close))
    profile = _limited()
    profile["strategy"]["exit_signal"]["any"] = ["close < 0"]  # only stops can exit
    profile["strategy"]["flash_crash_buy"] = {"enabled": True, "reserve": 100,
                                              "levels": [{"drop": 0.3, "allocation": 0.5},
                                                         {"drop": 0.6, "allocation": 0.5}]}
    return run_backtest(candles, profile), candles


def test_a_crash_that_recovers_within_an_hour_is_ignored_and_the_reserve_sells_on_recovery():
    result, candles = _holding_through([50, 45], recovery_close=104)  # 30 minutes down, then back

    assert result["flash_crash_events"][0]["brief"] is True
    assert result["open_position"] is True  # no stop fired; the crash was treated as bad data
    assert result["max_window_drawdown"] < 0.02  # valuation ignored the crash prices too
    fills = result["flash_crash_buys"]
    assert [fill["entry_price"] for fill in fills] == pytest.approx([105 * 0.7, 105 * 0.4])
    assert all(fill["exit_time"] == candles[-1]["start"] for fill in fills)  # sold at the recovery close


def test_a_crash_that_holds_longer_than_an_hour_is_held_through_until_it_recovers():
    result, candles = _holding_through([50, 48, 47, 46, 45, 44], recovery_close=100)  # 90 minutes down

    event = result["flash_crash_events"][0]
    assert event["brief"] is False and event["minutes"] == 105
    assert result["open_position"] is True  # nothing sold during the crash
    assert result["max_window_drawdown"] > 0.10  # the real loss shows while it lasted
    assert all(fill["exit_time"] == candles[-1]["start"] for fill in result["flash_crash_buys"])


def test_a_crash_that_never_recovers_keeps_everything_held():
    result, _ = _holding_through([50, 45, 40, 35, 30, 25])

    assert result["flash_crash_events"][0]["minutes"] is None
    assert result["open_position"] is True
    assert result["flash_crash_buys"] == [] and result["open_flash_lots"] == 2


def test_raw_anomalies_are_kept_so_a_stop_gapped_through_fills_at_the_print():
    candles = [_candle(i * STEP, p) for i, p in enumerate([100] * 6 + [101, 105])]
    candles.append(_candle(8 * STEP, 104, low=0.06, open_=0.06))  # an erroneous-looking print, left as is

    result = run_backtest(candles, _limited())

    assert result["trades"][0]["exit_reason"] == "DRAWDOWN_STOP"
    assert result["trades"][0]["exit_price"] == pytest.approx(0.06)

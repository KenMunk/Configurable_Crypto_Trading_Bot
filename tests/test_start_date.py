from datetime import datetime, timezone

import pytest

from backtest import run_backtest
from simulator import format_buy_and_hold, format_test_start, parse_start_date
from tests.test_backtest import _candle, _profile

STEP = 900
# Flat, then an uptrend from candle 6, then a dip that ends it, then a second uptrend.
PRICES = [100] * 6 + [101, 102, 103, 104, 105, 106] + [100, 95, 90] + [92, 95, 98, 101, 104, 107, 110]


def _candles():
    return [_candle(i * STEP, price) for i, price in enumerate(PRICES)]


def test_start_time_begins_the_test_later_with_warmed_up_indicators():
    start = 9 * STEP  # mid-uptrend; the 5-candle slow EMA is already formed from earlier candles

    result = run_backtest(_candles(), _profile(), start_time=start)

    assert result["period_start"] == start
    assert result["series"]["time"][0] == start
    assert result["series"]["equity"][0] == result["starting_capital"]
    assert result["warmup_candles"] == 9
    # Entry rules already hold at the start, so the model buys on the very first candle.
    assert result["first_entry"] == start
    assert result["trades"][0]["entry_price"] == PRICES[9]
    assert result["buy_and_hold_return"] == pytest.approx(110 / PRICES[9] - 1)


def test_waiting_for_a_fresh_signal_skips_the_trend_already_underway():
    start = 9 * STEP

    result = run_backtest(_candles(), _profile(), start_time=start, wait_for_fresh_signal=True)

    assert result["waited_for_fresh_signal"] is True
    assert result["first_entry"] > 14 * STEP  # after the dip ended the first trend
    assert all(trade["entry_time"] > 14 * STEP for trade in result["trades"])


def test_buy_and_hold_starts_at_the_models_first_buy():
    start = 9 * STEP
    result = run_backtest(_candles(), _profile(), start_time=start, wait_for_fresh_signal=True)

    first_buy = result["first_entry"]
    assert first_buy > start  # it waited out the first trend
    assert result["buy_and_hold_start"] == first_buy
    assert result["buy_and_hold_return"] == pytest.approx(110 / PRICES[first_buy // STEP] - 1)
    assert result["buy_and_hold_from_test_start"] == pytest.approx(110 / PRICES[9] - 1)


def test_buy_and_hold_starts_at_the_test_start_when_the_model_never_buys():
    profile = _profile()
    profile["strategy"]["entry_signal"] = {"all": ["close < 0"]}

    result = run_backtest(_candles(), profile, start_time=9 * STEP)

    assert result["first_entry"] is None
    assert result["buy_and_hold_start"] == 9 * STEP
    assert result["buy_and_hold_return"] == pytest.approx(110 / PRICES[9] - 1)


def test_report_says_where_buy_and_hold_starts():
    bought = run_backtest(_candles(), _profile(), start_time=9 * STEP, wait_for_fresh_signal=True)
    lines = format_buy_and_hold(bought)
    assert lines[0].startswith("Buy and hold return: +") and "at the model's first buy, 1970-01-01" in lines[0]
    assert lines[1].startswith("Buy and hold from the test start: +")

    profile = _profile()
    profile["strategy"]["entry_signal"] = {"all": ["close < 0"]}
    never = format_buy_and_hold(run_backtest(_candles(), profile, start_time=9 * STEP))
    assert never == [f"Buy and hold return: {110 / PRICES[9] - 1:+.2%} (from the test start, because the model never bought)"]


def test_start_before_the_data_uses_the_earliest_candle_and_after_it_is_an_error():
    early = run_backtest(_candles(), _profile(), start_time=-10 * STEP)
    assert early["period_start"] == 0 and early["warmup_candles"] == 0

    with pytest.raises(ValueError, match="after the last candle"):
        run_backtest(_candles(), _profile(), start_time=len(PRICES) * STEP)


def test_no_start_time_tests_the_whole_history():
    result = run_backtest(_candles(), _profile())

    assert result["requested_start"] is None
    assert result["period_start"] == 0
    assert format_test_start(result) == []


def test_parse_start_date():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    assert parse_start_date("", now) is None
    assert parse_start_date("2020-01-01", now) == int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    assert parse_start_date(" 2020-01-01 14:30 ", now) == int(
        datetime(2020, 1, 1, 14, 30, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError, match="must look like"):
        parse_start_date("01/02/2020", now)
    with pytest.raises(ValueError, match="in the future"):
        parse_start_date("2027-01-01", now)


def test_report_describes_the_start_warmup_and_first_entry():
    result = run_backtest(_candles(), _profile(), start_time=9 * STEP, wait_for_fresh_signal=True)

    lines = format_test_start(result)

    assert lines[0].startswith("Test start: 1970-01-01 02:15 UTC")
    assert "warm-up" in lines[1]
    assert lines[2].startswith("First entry: ") and "waited for a fresh entry signal" in lines[2]

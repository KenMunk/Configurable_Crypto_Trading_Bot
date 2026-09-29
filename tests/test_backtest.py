from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backtest import (
    MAX_CANDLES_PER_REQUEST,
    fetch_candles,
    load_candles,
    lookback_window,
    resolve_granularity,
    run_backtest,
)
from simulator import format_backtest_result, trade_rows, trade_summary

MINUTE = 60


class FakeCandleClient:
    """Serves a flat-priced candle for every minute, recording each request."""

    def __init__(self):
        self.requests = []

    def get_candles(self, product_id, start, end, granularity, limit):
        self.requests.append((int(start), int(end)))
        first = (int(start) // MINUTE + 1) * MINUTE
        candles = [
            SimpleNamespace(start=str(t), open="1", high="1", low="1", close="1", volume="1")
            for t in range(first, int(end) + 1, MINUTE)
        ]
        # The API returns newest first.
        return SimpleNamespace(candles=list(reversed(candles))[:limit])


def _candle(start, close, low=None, high=None, open_=None):
    return {
        "start": start,
        "open": close if open_ is None else open_,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 1.0,
    }


def _profile(**overrides):
    profile = {
        "product_id": "BTC-USD",
        "strategy": {
            "direction": "LONG_ONLY",
            "entry_signal": {"all": ["fast_ema > slow_ema"]},
            "exit_signal": {"any": ["fast_ema < slow_ema"], "hard_stop_trigger": True},
        },
        "indicators": {
            "fast_ema": {"type": "EMA", "period": 2},
            "slow_ema": {"type": "EMA", "period": 5},
            "emergency_ema": {"type": "EMA", "period": 1},
        },
        "friction_model": {"exchange_commission_rate": 0.0, "slippage_buffer": 0.0},
        "capital_allocation": {"target_position_fraction": 1.0},
        "safeguards": {"use_hard_stop": False},
    }
    profile.update(overrides)
    return profile


def test_resolve_granularity_accepts_api_names_and_doc_aliases():
    assert resolve_granularity("FIFTEEN_MINUTE") == ("FIFTEEN_MINUTE", 900)
    assert resolve_granularity("hourly") == ("ONE_HOUR", 3600)
    with pytest.raises(ValueError):
        resolve_granularity("WEEKLY")


def test_lookback_window_spans_ten_years_on_candle_boundaries():
    now = datetime(2026, 9, 28, 12, 7, tzinfo=timezone.utc)

    start, end = lookback_window(900, now=now)

    assert datetime.fromtimestamp(start, timezone.utc) == datetime(2016, 9, 28, 12, 0, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(end, timezone.utc) == datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_fetch_candles_pages_without_gaps_or_duplicates():
    client = FakeCandleClient()
    start = 1_700_000_040
    end = start + 1000 * MINUTE
    fractions = []

    candles = fetch_candles(
        client, "BTC-USD", "ONE_MINUTE", start, end,
        progress=lambda fraction, message: fractions.append(fraction), pause=0,
    )

    assert [c["start"] for c in candles] == list(range(start, end, MINUTE))
    assert len(client.requests) == -(-1000 // (MAX_CANDLES_PER_REQUEST - 1))
    assert fractions[-1] == 1.0


def test_load_candles_reuses_cache_and_fetches_only_new_candles(tmp_path):
    start = 1_700_000_040
    end = start + 500 * MINUTE
    client = FakeCandleClient()
    first = load_candles(client, "BTC-USD", "ONE_MINUTE", start, end, cache_dir=tmp_path)
    first_requests = len(client.requests)

    cached_client = FakeCandleClient()
    messages = []
    again = load_candles(
        cached_client, "BTC-USD", "ONE_MINUTE", start, end, cache_dir=tmp_path,
        progress=lambda fraction, message: messages.append(message),
    )

    assert again == first
    assert cached_client.requests == []
    assert "cache" in messages[-1]

    later_client = FakeCandleClient()
    later_messages = []
    later = load_candles(
        later_client, "BTC-USD", "ONE_MINUTE", start + 10 * MINUTE, end + 10 * MINUTE, cache_dir=tmp_path,
        progress=lambda fraction, message: later_messages.append(message),
    )

    assert later_messages[-1] == "500 candles (490 from cache, 10 new)"

    assert first_requests > 1
    assert later_client.requests == [(end - 1, end + 10 * MINUTE)]
    assert [c["start"] for c in later] == list(range(start + 10 * MINUTE, end + 10 * MINUTE, MINUTE))


def test_backtest_enters_on_uptrend_and_exits_on_trend_reversal():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    progress = []

    result = run_backtest(candles, _profile(), progress=lambda fraction, message: progress.append(fraction))

    assert result["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["entry_price"] == 101
    assert trade["exit_reason"] == "EXIT_SIGNAL"
    assert result["open_position"] is False
    assert progress[-1] == 1.0


def test_backtest_hard_stop_fills_at_stop_price():
    prices = [100] * 6 + [101, 102]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    candles.append(_candle(len(candles) * 900, 99, low=90, open_=101))
    profile = _profile(safeguards={"use_hard_stop": True, "hard_stop_percentage": 0.05})

    result = run_backtest(candles, profile)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "HARD_STOP"
    assert trade["exit_price"] == pytest.approx(101 * 0.95)


def test_backtest_pauses_on_drawdown_limit_until_a_fresh_trend_signal():
    prices = [100] * 6 + [101, 102, 103] + [90, 88] + [89, 95, 100, 105, 110]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    profile = _profile(safeguards={"use_hard_stop": False, "max_drawdown_limit": 0.05})

    result = run_backtest(candles, profile)

    assert len(result["drawdown_pauses"]) == 1
    pause = result["drawdown_pauses"][0]
    assert pause["paused_at"] == 9 * 900
    assert result["trades"][0]["exit_time"] == 9 * 900
    # Trading resumes with the next entry after the trend reset, not before.
    assert pause["resumed_at"] is not None
    assert result["open_position"] is True
    assert result["series"]["indicators"]["fast_ema"][10] <= result["series"]["indicators"]["slow_ema"][10]
    assert pause["resumed_at"] > 10 * 900


def test_backtest_stays_paused_while_the_breached_trend_continues():
    # The drop breaches the limit but the fast EMA never falls below the slow one,
    # so there is no fresh signal and no re-entry.
    prices = [100] * 6 + [110, 120, 130, 140, 150, 142, 150, 160, 170]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    profile = _profile(safeguards={"use_hard_stop": False, "max_drawdown_limit": 0.04})

    result = run_backtest(candles, profile)

    assert [p["resumed_at"] for p in result["drawdown_pauses"]] == [None]
    assert result["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "DRAWDOWN_LIMIT"
    assert result["open_position"] is False


def test_backtest_charges_commission_on_both_fills():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    profile = _profile(friction_model={"exchange_commission_rate": 0.01, "slippage_buffer": 0.0})

    result = run_backtest(candles, profile)

    assert result["fees_paid"] > 0
    assert result["ending_equity"] < result["starting_capital"] * (1 + result["trades"][0]["return"]) + 1e-9


def test_format_backtest_result_includes_headline_metrics():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    result = run_backtest([_candle(i * 900, p) for i, p in enumerate(prices)], _profile())

    text = format_backtest_result(result)

    assert "Total return" in text
    assert "EXIT_SIGNAL" in text
    assert "Not modeled yet" in text
    assert "Friction model:" in text and "Taxes paid" in text


def test_backtest_records_model_value_for_every_candle():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    result = run_backtest([_candle(i * 900, p) for i, p in enumerate(prices)], _profile())

    equity = result["series"]["equity"]
    assert len(equity) == len(prices)
    assert equity[0] == result["starting_capital"]
    assert equity[-1] == result["ending_equity"]
    assert result["trades"][0]["equity_after"] == result["ending_equity"]


def test_starting_value_comes_from_the_profile():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    profile = _profile()
    profile["capital_allocation"]["starting_capital"] = 2_500

    result = run_backtest(candles, profile)

    assert result["starting_capital"] == 2_500
    assert result["series"]["equity"][0] == 2_500
    assert result["trades"][0]["position_cost"] == 2_500  # full-size position from the start
    assert run_backtest(candles, profile, starting_capital=1_000)["starting_capital"] == 1_000
    assert run_backtest(candles, _profile())["starting_capital"] == 10_000  # default


@pytest.mark.parametrize("bad", [0, -5, "10k", True])
def test_invalid_starting_value_is_rejected(bad):
    profile = _profile()
    profile["capital_allocation"]["starting_capital"] = bad

    with pytest.raises(ValueError, match="starting_capital must be a positive number"):
        run_backtest([_candle(0, 100)], profile)


def test_trade_rows_and_summary_describe_each_outcome():
    trades = [
        {"entry_time": 0, "exit_time": 2 * 86_400 + 3_600, "entry_price": 100.0, "exit_price": 110.0,
         "position_cost": 2_500.0, "fees": 45.0, "pnl": 240.0, "return": 0.096, "equity_after": 10_240.0,
         "entry_reason": "EMA_TREND", "exit_reason": "PROFIT_LOCK"},
        {"entry_time": 3 * 86_400, "exit_time": 3 * 86_400 + 2_700, "entry_price": 110.0,
         "exit_price": 106.7, "position_cost": 2_560.0, "fees": 45.5, "pnl": -90.0, "return": -0.035,
         "equity_after": 10_150.0, "entry_reason": "EMA_TREND", "exit_reason": "HARD_STOP"},
    ]

    rows = trade_rows(trades)
    summary = trade_summary(trades)

    assert rows[0]["display"]["held"] == "2d 1h"
    assert rows[1]["display"]["held"] == "45m"
    assert [row["display"]["outcome"] for row in rows] == ["Win", "Loss"]
    assert rows[1]["display"]["pnl"] == "-90.00"
    assert sorted(rows, key=lambda row: row["sort"]["return"])[0] is rows[1]
    assert "2 trades: 1 wins, 1 losses (50.0% win rate)" in summary
    assert "Net P&L +150.00" in summary
    assert "PROFIT_LOCK 1" in summary and "HARD_STOP 1" in summary
    assert trade_summary([]) == "No trades were made in this simulation"

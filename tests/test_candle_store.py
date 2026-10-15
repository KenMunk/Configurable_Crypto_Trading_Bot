import contextlib
import csv
import json
import sqlite3

import numpy as np
import pytest

import candle_store
from backtest import load_cached_candles, run_backtest
from candle_store import CHECKED_CANDLES, CandleStore
from indicators import INDICATORS, WINDOW_LOOKBACK, IndicatorContext, output_names
from tests.test_backtest import _profile
from tests.test_indicators import FIFTEEN_MINUTES, _assert_close, _candles

PRODUCT, GRANULARITY = "BTC-USD", "FIFTEEN_MINUTE"
TABLE = f"{PRODUCT}_{GRANULARITY}"
CCI = {"cci": {"type": "CCI", "period": 20}}


def _save(tmp_path, candles):
    CandleStore(tmp_path).save(PRODUCT, GRANULARITY, candles, 0, candles[-1]["start"] + FIFTEEN_MINUTES)


def _loaded(tmp_path):
    return load_cached_candles(tmp_path, PRODUCT, GRANULARITY)[0]


def _query(tmp_path, sql):
    with contextlib.closing(sqlite3.connect(tmp_path / "candles.db")) as connection:
        return connection.execute(sql).fetchall()


def _columns(tmp_path):
    return [row[1] for row in _query(tmp_path, f'PRAGMA table_info("{TABLE}")')]


def _column(tmp_path, column):
    rows = _query(tmp_path, f'SELECT "{column}" FROM "{TABLE}" ORDER BY start')
    return np.array([np.nan if value is None else value for (value,) in rows])


def _evaluate(candles, definitions=CCI, name="cci"):
    """The series computed on a stored market's shared cache."""
    return IndicatorContext(candles.metrics.cache(candles), definitions, FIFTEEN_MINUTES).series(name)


def _fresh(candles, definitions=CCI, name="cci"):
    return IndicatorContext(list(candles), definitions, FIFTEEN_MINUTES).series(name)


def _spy(monkeypatch, indicator_type):
    """Record the number of candles each computation of the indicator type covers."""
    lengths = []
    function, *rest = INDICATORS[indicator_type]

    def spy(context, parameters):
        lengths.append(len(context.close))
        return function(context, parameters)

    monkeypatch.setitem(INDICATORS, indicator_type, (spy, *rest))
    return lengths


@pytest.fixture
def store_every_metric(monkeypatch):
    monkeypatch.setattr(candle_store, "STORE_SECONDS_PER_CANDLE", -1.0)


@pytest.fixture
def store_no_metric(monkeypatch):
    monkeypatch.setattr(candle_store, "STORE_SECONDS_PER_CANDLE", 1.0)


WINDOWED = {
    "SMA": {"period": 7}, "WMA": {"period": 7}, "HMA": {"period": 9}, "VWMA": {"period": 7},
    "STOCHASTIC": {"k_period": 9, "d_period": 3, "smooth": 2}, "ROC": {"period": 5}, "MOMENTUM": {"period": 5},
    "CCI": {"period": 7}, "WILLIAMS_R": {"period": 7}, "BOLLINGER": {"period": 7}, "DONCHIAN": {"period": 7},
    "STDDEV": {"period": 7}, "VOLATILITY": {"period": 7}, "MFI": {"period": 7},
}


@pytest.mark.parametrize("definitions", [
    *({"x": {"type": indicator_type, **parameters}} for indicator_type, parameters in WINDOWED.items()),
    {"x": {"type": "STOCHASTIC"}},
    {"cci": {"type": "CCI", "period": 6}, "x": {"type": "SMA", "period": 4, "source": "cci"}},
    {"bands": {"type": "BOLLINGER", "period": 6}, "x": {"type": "ROC", "period": 3, "source": "bands.middle"}},
], ids=lambda definitions: "-".join(definition["type"] for definition in definitions.values()))
def test_window_lookback_is_exactly_what_each_indicator_depends_on(definitions):
    assert set(WINDOWED) == set(WINDOW_LOOKBACK)
    candles = _candles(300)
    lookback = IndicatorContext(candles, definitions, FIFTEEN_MINUTES).lookback("x")
    outputs = output_names({"x": definitions["x"]})
    full = {output: IndicatorContext(candles, definitions, FIFTEEN_MINUTES).series(output) for output in outputs}

    for first in (37, 150):
        part = IndicatorContext(candles[first:], definitions, FIFTEEN_MINUTES)
        for output in outputs:
            rows = lookback[output.partition(".")[2] or None]
            values = part.series(output)
            assert np.isnan(values[:rows - 1]).all() and not np.isnan(values[rows - 1]), output
            _assert_close(values[rows - 1:], full[output][first + rows - 1:])


def test_history_dependent_indicators_have_no_lookback():
    definitions = {"rsi": {"type": "RSI"}, "smooth_rsi": {"type": "SMA", "period": 5, "source": "rsi"},
                   "cci": {"type": "CCI", "period": 20}, "smooth_cci": {"type": "SMA", "period": 5, "source": "cci"}}
    context = IndicatorContext(_candles(10), definitions, FIFTEEN_MINUTES)

    assert context.lookback("rsi") is None and context.lookback("smooth_rsi") is None
    assert context.lookback("smooth_cci") == {None: 5 + 20 - 1}


def test_metric_key_names_a_metric_the_same_way_in_every_profile():
    definitions = {
        "slow_sma": {"type": "SMA", "period": "30d"}, "trend": {"type": "SMA", "period": 2880, "source": "close"},
        "bands": {"type": "BOLLINGER", "period": 20, "std_dev": 2}, "wide": {"type": "BOLLINGER", "std_dev": 2.0},
        "rsi": {"type": "RSI"}, "smooth": {"type": "EMA", "period": 9, "source": "rsi", "label": "Smoothed"},
    }
    context = IndicatorContext(_candles(10), definitions, FIFTEEN_MINUTES)

    assert context.metric_key("slow_sma") == context.metric_key("trend") == "SMA(period=2880, source=close)"
    assert context.metric_key("bands.upper") == context.metric_key("wide.upper") == (
        "BOLLINGER(period=20, std_dev=2, source=close).upper")
    assert context.metric_key("smooth") == "EMA(period=9, source=RSI(period=14, source=close))"


def test_a_csv_cache_from_an_earlier_version_is_imported(tmp_path):
    candles = _candles(50)
    with (tmp_path / f"{TABLE}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["start", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(candles)
    (tmp_path / f"{TABLE}.meta.json").write_text(json.dumps({"fetched_start": 0, "fetched_end": 50 * 900}))

    loaded, fetched_start, fetched_end = load_cached_candles(tmp_path, PRODUCT, GRANULARITY)

    assert loaded == candles and (fetched_start, fetched_end) == (0, 50 * 900)
    (tmp_path / f"{TABLE}.csv").unlink()
    assert _loaded(tmp_path) == candles  # now read from the database


def test_a_slow_metric_gets_a_column_and_later_only_new_candles_are_computed(
        tmp_path, monkeypatch, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles[:300])
    _evaluate(_loaded(tmp_path))

    assert _columns(tmp_path)[6:] == ["CCI(period=20)"]
    _assert_close(_column(tmp_path, "CCI(period=20)"), _fresh(candles[:300]))

    _save(tmp_path, candles[300:])
    assert np.isnan(_column(tmp_path, "CCI(period=20)")[300:]).all()  # blank until a model needs it again

    expected = _fresh(candles)
    lengths = _spy(monkeypatch, "CCI")
    values = _evaluate(_loaded(tmp_path))

    # Only the 100 new candles are computed, from 19 earlier ones to warm up and 50 to check.
    assert lengths == [100 + CHECKED_CANDLES + 19]
    _assert_close(values, expected)
    _assert_close(_column(tmp_path, "CCI(period=20)"), expected)
    _assert_close(_evaluate(_loaded(tmp_path)), expected)
    assert lengths == [169]  # complete now, so read back without computing


def test_fast_and_history_dependent_metrics_are_not_stored(tmp_path, monkeypatch):
    _save(tmp_path, _candles(300))
    definitions = {"cci": {"type": "CCI", "period": 20}, "ema": {"type": "EMA", "period": 20},
                   "rsi": {"type": "RSI"}, "smooth_rsi": {"type": "SMA", "period": 5, "source": "rsi"}}

    monkeypatch.setattr(candle_store, "STORE_SECONDS_PER_CANDLE", 1.0)
    _evaluate(_loaded(tmp_path), definitions, "cci")
    assert _columns(tmp_path)[6:] == []  # faster to compute than to read back

    monkeypatch.setattr(candle_store, "STORE_SECONDS_PER_CANDLE", -1.0)
    for name in ("ema", "smooth_rsi"):
        _evaluate(_loaded(tmp_path), definitions, name)
    assert _columns(tmp_path)[6:] == []  # these depend on the whole history before them


def test_a_later_window_reads_stored_values_with_its_own_warm_up(tmp_path, monkeypatch, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles)
    loaded = _loaded(tmp_path)
    _evaluate(loaded)
    expected = _fresh(candles[100:])
    lengths = _spy(monkeypatch, "CCI")

    later = loaded[100:]
    _assert_close(IndicatorContext(loaded.metrics.cache(later), CCI, FIFTEEN_MINUTES).series("cci"), expected)
    assert lengths == []


def test_a_window_reaching_before_the_stored_values_recomputes_them(tmp_path, monkeypatch, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles)
    loaded = _loaded(tmp_path)
    IndicatorContext(loaded.metrics.cache(loaded[100:]), CCI, FIFTEEN_MINUTES).series("cci")
    assert np.isnan(_column(tmp_path, "CCI(period=20)")[:119]).all()

    expected = _fresh(candles)
    lengths = _spy(monkeypatch, "CCI")
    _assert_close(_evaluate(_loaded(tmp_path)), expected)
    assert lengths == [400]
    _assert_close(_column(tmp_path, "CCI(period=20)"), expected)


def test_a_stored_value_that_no_longer_matches_is_recomputed(tmp_path, monkeypatch, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles[:300])
    _evaluate(_loaded(tmp_path))
    with contextlib.closing(sqlite3.connect(tmp_path / "candles.db")) as connection, connection:
        connection.execute(f'UPDATE "{TABLE}" SET "CCI(period=20)" = 12345 WHERE start = ?', (290 * 900,))
    _save(tmp_path, candles[300:])
    expected = _fresh(candles)
    lengths = _spy(monkeypatch, "CCI")

    values = _evaluate(_loaded(tmp_path))

    assert lengths == [169, 400]  # the check failed, so the whole column was recomputed
    _assert_close(values, expected)
    _assert_close(_column(tmp_path, "CCI(period=20)"), expected)


def _cci_profile(**overrides):
    profile = _profile(**overrides)
    profile["indicators"] = {"cci": {"type": "CCI", "period": 20}, "trend": {"type": "EMA", "period": 30}}
    profile["strategy"]["entry_signal"] = {"all": ["cci < -50"]}
    profile["strategy"]["exit_signal"] = {"any": ["cci > 50"]}
    return profile


def _same_result(stored, fresh):
    assert stored["trades"] == fresh["trades"] and stored["trade_count"] > 0
    assert stored["ending_equity"] == fresh["ending_equity"]
    for name, values in fresh["series"]["indicators"].items():
        _assert_close(stored["series"]["indicators"][name], values)


def test_backtests_give_the_same_results_with_stored_metrics(tmp_path, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles[:300])
    _same_result(run_backtest(_loaded(tmp_path), _cci_profile()), run_backtest(candles[:300], _cci_profile()))

    _save(tmp_path, candles[300:])
    _same_result(run_backtest(_loaded(tmp_path), _cci_profile()), run_backtest(candles, _cci_profile()))
    assert _columns(tmp_path)[6:] == ["CCI(period=20)"]


def test_models_on_the_same_candles_compute_a_shared_metric_once(tmp_path, monkeypatch, store_no_metric):
    _save(tmp_path, _candles(300))
    first, second = _profile(), _profile()
    first["indicators"] = {"fast": {"type": "SMA", "period": 3}, "slow": {"type": "SMA", "period": 6}}
    first["strategy"]["entry_signal"] = {"all": ["fast > slow"]}
    first["strategy"]["exit_signal"] = {"any": ["fast < slow"]}
    second["indicators"] = {"quick": {"type": "SMA", "period": "45m"}, "steady": {"type": "SMA", "period": 6}}
    second["strategy"]["entry_signal"] = {"all": ["quick > steady"]}
    second["strategy"]["exit_signal"] = {"any": ["quick < steady"]}
    lengths = _spy(monkeypatch, "SMA")

    candles = _loaded(tmp_path)
    results = [run_backtest(candles, profile) for profile in (first, second)]

    assert lengths == [300, 300]  # one SMA 3 and one SMA 6, not one per model
    assert results[0]["trades"] == results[1]["trades"]


def test_flash_crash_profiles_see_the_crash_and_share_the_stored_columns(tmp_path, store_every_metric):
    candles = _candles(400)
    crash = dict(candles[200], low=candles[199]["close"] * 0.4)  # a 60% wick that recovers within the candle
    candles[200] = crash
    _save(tmp_path, candles)
    flash = _cci_profile()
    flash["strategy"]["flash_crash_buy"] = {"levels": [{"drop": 0.5, "allocation": 0.5}]}

    with_flash = run_backtest(_loaded(tmp_path), flash)
    _same_result(with_flash, run_backtest(candles, flash))
    _same_result(run_backtest(_loaded(tmp_path), _cci_profile()), run_backtest(candles, _cci_profile()))

    assert _columns(tmp_path)[6:] == ["CCI(period=20)"]
    _assert_close(with_flash["series"]["indicators"]["cci"], _column(tmp_path, "CCI(period=20)"))


def _read_only(tmp_path):
    return load_cached_candles(tmp_path, PRODUCT, GRANULARITY, read_only=True)[0]


def test_a_read_only_store_never_creates_or_imports_a_database(tmp_path):
    (tmp_path / f"{TABLE}.csv").write_text("start,open,high,low,close,volume\n0,1,1,1,1,1\n", encoding="utf-8")
    (tmp_path / f"{TABLE}.meta.json").write_text(json.dumps({"fetched_start": 0, "fetched_end": 900}),
                                                 encoding="utf-8")
    store = CandleStore(tmp_path, read_only=True)

    assert store.load(PRODUCT, GRANULARITY) is None
    assert not (tmp_path / "candles.db").exists()
    with pytest.raises(PermissionError):
        store.save(PRODUCT, GRANULARITY, _candles(10), 0, 9000)
    _save(tmp_path, _candles(10))
    assert store.load("ETH-USD", GRANULARITY) is None  # the database exists, but not this market


def test_a_read_only_store_holds_metric_columns_back_for_the_writer(tmp_path, store_every_metric):
    candles = _candles(300)
    _save(tmp_path, candles)
    loaded = _read_only(tmp_path)

    _assert_close(_evaluate(loaded), _fresh(candles))

    assert _columns(tmp_path)[6:] == []  # nothing written
    pending = loaded.metrics.store.take_pending_metrics()
    assert [(table, list(columns)) for table, columns, _ in pending] == [(TABLE, ["CCI(period=20)"])]
    assert loaded.metrics.store.take_pending_metrics() == []
    CandleStore(tmp_path).apply_metrics(pending)
    _assert_close(_column(tmp_path, "CCI(period=20)"), _fresh(candles))


def test_a_read_only_reader_extends_a_stored_metric_through_the_writer(tmp_path, monkeypatch, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles[:300])
    _evaluate(_loaded(tmp_path))
    _save(tmp_path, candles[300:])
    expected = _fresh(candles)
    lengths = _spy(monkeypatch, "CCI")

    loaded = _read_only(tmp_path)
    _assert_close(_evaluate(loaded), expected)
    pending = loaded.metrics.store.take_pending_metrics()
    assert [extend for _, _, extend in pending] == [True]
    CandleStore(tmp_path).apply_metrics(pending)

    _assert_close(_column(tmp_path, "CCI(period=20)"), expected)
    _evaluate(_loaded(tmp_path))
    assert lengths == [100 + CHECKED_CANDLES + 19]  # read back complete afterwards


def test_an_extension_that_no_longer_joins_the_stored_range_is_skipped(tmp_path, store_every_metric):
    candles = _candles(400)
    _save(tmp_path, candles)
    _evaluate(_loaded(tmp_path))
    times = np.array([candle["start"] for candle in candles])
    # A run whose window starts later replaced the column; an older reader's extension follows.
    CandleStore(tmp_path).write_metrics(TABLE, {"CCI(period=20)": (times[100:200], np.ones(100))})

    CandleStore(tmp_path).write_metrics(TABLE, {"CCI(period=20)": (times[300:], np.ones(100))}, extend=True)

    assert _query(tmp_path, "SELECT computed_from, computed_through FROM metric_columns") == [
        (int(times[100]), int(times[199]))]
    assert np.isnan(_column(tmp_path, "CCI(period=20)")[300:]).all()


def test_readers_see_the_last_commit_while_a_write_is_under_way(tmp_path):
    candles = _candles(20)
    _save(tmp_path, candles[:10])
    writer = sqlite3.connect(tmp_path / "candles.db", isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f'INSERT INTO "{TABLE}" (start, close) VALUES (999999, 1)')
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)

        assert len(_read_only(tmp_path)) == 10  # not blocked, and the uncommitted candle isn't seen
    finally:
        writer.execute("ROLLBACK")
        writer.close()

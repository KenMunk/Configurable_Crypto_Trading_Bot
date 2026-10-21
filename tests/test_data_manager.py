import numpy as np
import pytest

import synthetic
from candle_store import CandleStore

DAY = 86_400


def _store(tmp_path):
    store = CandleStore(tmp_path)
    for product, granularity, count in (("BTC-USD", "ONE_DAY", 30), ("ETH-USD", "ONE_DAY", 20),
                                        ("BTC-USD", "SIX_HOUR", 40)):
        step = DAY if granularity == "ONE_DAY" else DAY // 4
        candles = [{"start": i * step, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0 + i, "volume": 1.0}
                   for i in range(count)]
        store.save(product, granularity, candles, 0, count * step)
        starts = np.array([candle["start"] for candle in candles])
        store.write_metrics(store.table_name(product, granularity), {"sma_5": (starts, np.ones(count))})
    return store


def test_clearing_markets_removes_their_candles_indicators_and_old_csv_cache(tmp_path):
    store = _store(tmp_path)
    markets = {market["table"]: market for market in store.markets()}
    assert markets["BTC-USD_ONE_DAY"]["candles"] == 30 and markets["BTC-USD_ONE_DAY"]["metrics"] == 1
    assert markets["BTC-USD_SIX_HOUR"]["last"] == 39 * DAY // 4
    (tmp_path / "BTC-USD_ONE_DAY.csv").write_text("start\n", encoding="utf-8")  # an earlier version's cache
    (tmp_path / "BTC-USD_ONE_DAY.meta.json").write_text("{}", encoding="utf-8")

    assert store.clear(["BTC-USD_ONE_DAY", "not-a-market"]) == ["BTC-USD_ONE_DAY"]

    assert [market["table"] for market in store.markets()] == ["BTC-USD_SIX_HOUR", "ETH-USD_ONE_DAY"]
    assert store.load("BTC-USD", "ONE_DAY") is None  # gone, so the next run downloads it
    assert store.metric_ranges("BTC-USD_ONE_DAY", ["sma_5"]) == {}
    assert not (tmp_path / "BTC-USD_ONE_DAY.csv").exists() and not (tmp_path / "BTC-USD_ONE_DAY.meta.json").exists()
    assert store.load("ETH-USD", "ONE_DAY")[0][-1]["close"] == 20.0  # the others are untouched

    assert store.clear() == ["BTC-USD_SIX_HOUR", "ETH-USD_ONE_DAY"]
    assert store.markets() == []
    with pytest.raises(PermissionError):
        CandleStore(tmp_path, read_only=True).clear()


def test_clearing_the_synthetic_store(tmp_path):
    store = synthetic.SyntheticStore(tmp_path / "synthetic")
    history = [{"start": i * DAY, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0 + i / 100, "volume": 1.0}
               for i in range(200)]
    for path in range(2):
        synthetic.market_candles(list(history), {"name": "c", "type": "crash", "drop": 0.3, "days": 5}, DAY, path,
                                 store=store, market=("BTC-USD", "ONE_DAY"))
    assert len(store.runs()) == 2 and store.size() > 0
    assert store.clear() == 2
    assert store.runs() == [] and store.size() == 0


@pytest.fixture
def dialog(tmp_path):
    tk = pytest.importorskip("tkinter")
    from data_manager import ClearDataDialog
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    answers, busy = [], [False]
    window = ClearDataDialog(root, store=_store(tmp_path), synthetic_store=synthetic.SyntheticStore(tmp_path / "syn"),
                             busy=lambda: busy[0], ask=lambda title, message: answers.pop(0) if answers else True)
    window.answers, window.busy_flag = answers, busy
    yield window
    root.destroy()


def test_the_dialog_lists_markets_and_clears_the_selected_ones_after_asking(dialog):
    from data_manager import describe_market
    assert list(dialog.table.get_children()) == ["BTC-USD_ONE_DAY", "BTC-USD_SIX_HOUR", "ETH-USD_ONE_DAY"]
    assert dialog.table.item("BTC-USD_ONE_DAY", "values")[:3] == ("BTC-USD", "ONE_DAY", "30")
    assert describe_market(dialog.markets["BTC-USD_SIX_HOUR"])[3:5] == ("1970-01-01", "1970-01-10")
    assert dialog.history_var.get().startswith("3 markets, 90 candles")
    assert dialog.clear_selected_button.instate(["disabled"])  # nothing selected yet
    assert dialog.synthetic_var.get() == "No synthetic candles stored"

    dialog.table.selection_set(["BTC-USD_ONE_DAY"])
    dialog.answers.append(False)  # "No" keeps everything
    dialog.clear_selected()
    assert len(dialog.table.get_children()) == 3

    dialog.busy_flag[0] = True  # a run is reading the data
    dialog.clear_selected()
    assert "running" in dialog.status_var.get() and len(dialog.table.get_children()) == 3

    dialog.busy_flag[0] = False
    dialog.clear_selected()
    assert list(dialog.table.get_children()) == ["BTC-USD_SIX_HOUR", "ETH-USD_ONE_DAY"]
    assert dialog.status_var.get() == "Cleared 1 market: BTC-USD ONE_DAY"

    dialog.clear_all()
    assert dialog.table.get_children() == () and dialog.history_var.get() == "No price history stored"
    assert dialog.clear_all_button.instate(["disabled"])

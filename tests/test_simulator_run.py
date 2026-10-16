import queue
from types import SimpleNamespace

import pytest

import parallel_backtest
from simulator import SimulationWindow, comparison_rows, group_by_market, model_names, parse_workers
from tests.test_backtest import FakeCandleClient, _profile


def _minute_profile(**changes):
    profile = _profile(period_granularity="ONE_MINUTE")
    profile.update(changes)
    return profile


def _worker_events(auth_result, tmp_path, monkeypatch, models=None, workers=1):
    downloads = []
    monkeypatch.setattr("simulator.load_candles", _fake_load_candles(tmp_path, downloads))
    monkeypatch.setattr("simulator.fetch_friction_inputs", lambda client, profiles: {})  # no network in tests
    window = SimulationWindow.__new__(SimulationWindow)  # the worker needs no Tk widgets
    window.progress_queue = queue.Queue()
    window.performance_log_path = tmp_path / "model_performance.csv"
    tested_clients = []

    def authenticate_coinbase(client=None, **kwargs):
        tested_clients.append(client)
        return auth_result

    window.auth_engine = SimpleNamespace(authenticate_coinbase=authenticate_coinbase)
    client = FakeCandleClient()
    window._simulation_worker(client, models or [("model_a", _minute_profile())], workers=workers)

    events = []
    while not window.progress_queue.empty():
        events.append(window.progress_queue.get())
    return events, tested_clients, client, downloads


def _fake_load_candles(tmp_path, downloads):
    from backtest import load_candles

    def load(client, product_id, granularity, start, end, progress=None):
        downloads.append((product_id, granularity))
        # A short window keeps the test fast; the real one spans 10 years.
        return load_candles(client, product_id, granularity, end - 30 * 60, end,
                            cache_dir=tmp_path, progress=progress)

    return load


def test_simulation_tests_the_api_before_gathering_data(tmp_path, monkeypatch):
    auth = {"authenticated": True, "account_count": 16, "message": "ok"}

    events, tested_clients, client, _ = _worker_events(auth, tmp_path, monkeypatch)

    kinds = [event[0] for event in events]
    assert kinds[:2] == ["progress", "auth"]
    assert "friction" in kinds and kinds.index("friction") > kinds.index("auth")
    assert events[0][1] == "api"
    assert tested_clients == [client]
    first_data = next(i for i, e in enumerate(events) if e[0] == "progress" and e[1] == "data")
    assert first_data > kinds.index("auth")
    assert kinds[-1] == "done"
    assert [result["model_name"] for result in events[-1][1]] == ["model_a"]
    assert events[-1][2] == "model_performance.csv: 1 added"
    assert (tmp_path / "model_performance.csv").read_text(encoding="utf-8").splitlines()[1].startswith("model_a,")


def test_simulation_stops_when_the_api_test_fails(tmp_path, monkeypatch):
    auth = {"authenticated": False, "account_count": 0, "message": "HTTP Error 401: Unauthorized"}

    events, _, client, downloads = _worker_events(auth, tmp_path, monkeypatch)

    assert [event[0] for event in events] == ["progress", "auth", "error"]
    assert "Coinbase API test failed: HTTP Error 401" in str(events[-1][1])
    assert client.requests == [] and downloads == []


def test_several_models_share_downloads_and_each_gets_a_result(tmp_path, monkeypatch):
    auth = {"authenticated": True, "account_count": 1, "message": "ok"}
    slower = _minute_profile()
    slower["indicators"]["slow_ema"]["period"] = 8
    models = [
        ("fast", _minute_profile()),
        ("slow", slower),
        ("five_minute", _profile(period_granularity="FIVE_MINUTE")),
    ]

    events, _, _, downloads = _worker_events(auth, tmp_path, monkeypatch, models)

    results = events[-1][1]
    assert events[-1][0] == "done"
    assert [result["model_name"] for result in results] == ["fast", "slow", "five_minute"]
    assert [result["granularity"] for result in results] == ["ONE_MINUTE", "ONE_MINUTE", "FIVE_MINUTE"]
    assert downloads == [("BTC-USD", "ONE_MINUTE"), ("BTC-USD", "FIVE_MINUTE")]  # one per market
    simulation = [event for event in events if event[0] == "progress" and event[1] == "simulation"]
    assert simulation[-1][2] == 1.0
    assert any("slow (2 of 3)" in event[3] for event in simulation)
    assert all(earlier[2] <= later[2] for earlier, later in zip(simulation, simulation[1:]))


def test_each_model_starts_with_its_own_configured_capital(tmp_path, monkeypatch):
    auth = {"authenticated": True, "account_count": 1, "message": "ok"}
    small, large = _minute_profile(), _minute_profile()
    small["capital_allocation"]["starting_capital"] = 2_500
    large["capital_allocation"]["starting_capital"] = 50_000

    events, _, _, _ = _worker_events(auth, tmp_path, monkeypatch, [("small", small), ("large", large)])

    results = events[-1][1]
    assert [result["starting_capital"] for result in results] == [2_500, 50_000]
    assert [result["series"]["equity"][0] for result in results] == [2_500, 50_000]
    assert [row["display"]["start"] for row in comparison_rows(results)] == ["2,500.00", "50,000.00"]


def test_model_names_are_file_names_made_unique():
    assert model_names(["config/a.json", "other/b.json", "elsewhere/a.json"]) == ["a", "b", "a (2)"]


def test_group_by_market_uses_product_and_candle_size():
    models = [("a", {"product_id": "BTC-USD", "period_granularity": "HOURLY"}),
              ("b", {"product_id": "BTC-USD", "period_granularity": "ONE_HOUR"}),
              ("c", {"product_id": "ETH-USD", "period_granularity": "ONE_HOUR"})]

    assert group_by_market(models) == {("BTC-USD", "ONE_HOUR"): ["a", "b"], ("ETH-USD", "ONE_HOUR"): ["c"]}


def test_comparison_rows_format_headline_results():
    result = {"model_name": "trend", "product_id": "BTC-USD", "granularity": "FIFTEEN_MINUTE",
              "starting_capital": 10_000.0, "ending_equity": 921_352.4, "total_return": 91.135,
              "buy_and_hold_return": 136.5, "max_drawdown": 0.6, "trade_count": 14, "win_rate": 0.5,
              "max_window_drawdown": 0.1, "drawdown_window": "180d", "flash_crash_buys": [{}, {}],
              "fees_paid": 166_879.0, "taxes_paid": 520_494.0, "taxes_reserved": 500.0, "screened_signals": 0}

    row = comparison_rows([result])[0]

    assert row["display"]["first_buy"] == "never"  # this result has no first_entry
    assert row["display"]["bh_start"] == "test start"  # nor a buy_and_hold_start
    assert row["display"]["versus"] == "-33.0%"  # 92.135x against buy and hold's 137.5x
    assert row["sort"]["versus"] == pytest.approx(92.135 / 137.5 - 1)
    assert row["sort"]["first_buy"] == float("inf")
    assert row["display"]["end"] == "921,352.40"
    assert row["display"]["taxes"] == "520,994.00"
    assert row["display"]["return"] == "+9113.50%"
    assert row["display"]["max_drawdown"] == "60.0%"
    assert row["display"]["window_drawdown"] == "10.00% / 180d"
    assert row["display"]["flash"] == "2"
    assert row["sort"]["end"] == 921_352.4
    assert row["result"] is result


def test_parallel_workers_run_the_models_at_once_and_log_each(tmp_path, monkeypatch):
    auth = {"authenticated": True, "account_count": 1, "message": "ok"}
    end = 1_800_000_000 // 60 * 60
    monkeypatch.setattr(parallel_backtest, "lookback_window", lambda seconds: (end - 30 * 60, end))
    monkeypatch.setattr("simulator.run_parallel",
                        lambda *args, **kwargs: parallel_backtest.run_parallel(*args, cache_dir=tmp_path, **kwargs))
    broken = _minute_profile()
    broken["strategy"]["entry_signal"] = {"all": ["no_such_indicator > 1"]}
    models = [("a", _minute_profile()), ("b", _minute_profile()), ("broken", broken)]

    events, _, client, downloads = _worker_events(auth, tmp_path, monkeypatch, models, workers=2)

    assert events[-1][0] == "done"
    assert [result["model_name"] for result in events[-1][1]] == ["a", "b"]
    assert events[-1][2].startswith("model_performance.csv: 2 added; FAILED broken:")
    assert downloads == []  # the parallel run's own candle writer downloads, not the serial path
    assert len(client.requests) == 1  # one download for the market all three share
    phases = {event[1] for event in events if event[0] == "progress"}
    assert {"api", "data", "simulation"} <= phases


def test_parse_workers_defaults_to_every_cpu_but_two_and_never_exceeds_it(monkeypatch):
    monkeypatch.setattr(parallel_backtest, "cpu_count", lambda: 8)

    assert parse_workers("auto") == parse_workers("") == parse_workers(" MAX ") == 6
    assert parse_workers(" 4 ") == 4
    assert parse_workers("64") == 6
    for text in ("two", "0", "-1"):
        with pytest.raises(ValueError):
            parse_workers(text)
    for cpus in (1, 2, 3):
        monkeypatch.setattr(parallel_backtest, "cpu_count", lambda: cpus)
        assert parse_workers("auto") == 1  # a small computer still gets one worker

import csv

import pytest

import parallel_backtest
from backtest import load_cached_candles, run_backtest
from candle_store import CandleStore
from friction import FrictionModel
from parallel_backtest import effective_worker_count, resolve_compute_device, run_parallel
from tests.test_backtest import FakeCandleClient, _profile
from tests.test_indicators import FIFTEEN_MINUTES, _candles

CCI_PROFILE = _profile(
    indicators={"cci": {"type": "CCI", "period": 20}, "fast_ema": {"type": "EMA", "period": 2}},
    strategy={"direction": "LONG_ONLY", "entry_signal": {"all": ["cci < -50"]},
              "exit_signal": {"any": ["cci > 50"]}})


@pytest.fixture
def cached_markets(tmp_path, monkeypatch):
    """BTC-USD and ETH-USD 15-minute candles in a fresh cache, all inside the test window."""
    for product, seed in (("BTC-USD", 7), ("ETH-USD", 11)):
        candles = _candles(600, seed)
        CandleStore(tmp_path).save(product, "FIFTEEN_MINUTE", candles, 0, 600 * FIFTEEN_MINUTES)
    monkeypatch.setattr(parallel_backtest, "lookback_window", lambda seconds: (0, 600 * FIFTEEN_MINUTES))
    return tmp_path


def _models():
    return [("btc_ema", _profile()), ("btc_cci", CCI_PROFILE),
            ("eth_ema", _profile(product_id="ETH-USD")), ("eth_cci", {**CCI_PROFILE, "product_id": "ETH-USD"})]


def _serial(cache_dir, profile):
    candles = load_cached_candles(cache_dir, profile["product_id"], "FIFTEEN_MINUTE")[0]
    return run_backtest(candles, profile, friction=FrictionModel(profile))


def _log(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["model"]: row for row in csv.DictReader(handle)}


def test_parallel_results_match_one_after_another_and_every_model_is_logged(cached_markets):
    log = cached_markets / "model_performance.csv"

    run = run_parallel(None, _models(), workers=2, cache_dir=cached_markets, performance_path=log)

    assert run["errors"] == {}
    for (name, profile), result in zip(_models(), run["results"]):
        expected = _serial(cached_markets, profile)
        assert result["model_name"] == name and result["granularity"] == "FIFTEEN_MINUTE"
        assert result["ending_equity"] == pytest.approx(expected["ending_equity"])
        assert result["trade_count"] == expected["trade_count"]
        assert result["series"]["equity"] == pytest.approx(expected["series"]["equity"])
        assert result["series"]["tax_reserve"] == pytest.approx(expected["series"]["tax_reserve"])
    assert sorted(_log(log)) == ["btc_cci", "btc_ema", "eth_cci", "eth_ema"]
    assert run["logged"] == "model_performance.csv: 4 added"
    assert not any((cached_markets / "performance_shards").iterdir())  # every shard merged and removed


def test_a_failing_model_is_reported_without_stopping_the_others(cached_markets):
    log = cached_markets / "model_performance.csv"
    models = [("btc_ema", _profile()), ("sol_ema", _profile(product_id="SOL-USD"))]  # SOL-USD isn't cached

    run = run_parallel(None, models, workers=2, cache_dir=cached_markets, performance_path=log)

    assert run["results"][0] is not None and run["results"][1] is None
    assert "no cached candles" in run["errors"]["sol_ema"]
    assert list(_log(log)) == ["btc_ema"]


def test_the_candle_writer_downloads_before_the_workers_read(tmp_path, monkeypatch):
    end = 1_800_000_000 // 60 * 60
    monkeypatch.setattr(parallel_backtest, "lookback_window", lambda seconds: (end - 30 * 60, end))
    client = FakeCandleClient()
    models = [(f"m{i}", _profile(period_granularity="ONE_MINUTE")) for i in range(3)]
    phases = []
    shards_before = sorted(parallel_backtest.SHARD_DIR.rglob("*")) if parallel_backtest.SHARD_DIR.exists() else []

    run = run_parallel(client, models, workers=2, cache_dir=tmp_path, performance_path=None,
                       progress=lambda phase, fraction, message: phases.append((phase, fraction)))

    assert run["errors"] == {} and all(result["candle_count"] == 30 for result in run["results"])
    assert len(client.requests) == 1  # one download for the three models on the market
    assert ("data", 1.0) in phases and phases[-1] == ("simulation", 1.0)
    assert run["logged"] == "performance log not updated"
    # Without a log, no shards are written, here or in the real data folder.
    assert not (tmp_path / "performance_shards").exists()
    assert (sorted(parallel_backtest.SHARD_DIR.rglob("*")) if parallel_backtest.SHARD_DIR.exists() else []) == shards_before


def test_a_run_never_uses_more_workers_than_every_cpu_but_two(cached_markets, monkeypatch):
    monkeypatch.setattr(parallel_backtest, "cpu_count", lambda: 4)
    phases = []

    run_parallel(None, _models(), workers=16, cache_dir=cached_markets, performance_path=None,
                 progress=lambda phase, fraction, message: phases.append(message), compute_device="cpu")

    assert "0 of 4 models done on 2 workers" in phases


def test_gpu_slots_add_to_cpu_worker_capacity(monkeypatch):
    monkeypatch.setattr(parallel_backtest, "cpu_count", lambda: 6)

    assert effective_worker_count(10, workers=4, gpu_slots=2) == 6
    assert effective_worker_count(5, workers=4, gpu_slots=2) == 5
    assert effective_worker_count(10, workers=4, gpu_slots=0) == 4


def test_automatic_device_uses_gpus_when_available_and_otherwise_uses_cpu(monkeypatch):
    import gpu_indicators

    monkeypatch.setattr(gpu_indicators, "available_devices", lambda: [0, 1])
    assert resolve_compute_device("auto") == ("cuda", [0, 1])
    monkeypatch.setattr(gpu_indicators, "available_devices", lambda: [])
    assert resolve_compute_device("auto") == ("cpu", [])


def test_cuda_backtests_add_one_worker_slot_per_visible_gpu(cached_markets, monkeypatch):
    import gpu_indicators

    devices = gpu_indicators.available_devices()
    if not devices:
        pytest.skip("CUDA devices are unavailable")
    phases = []
    run = run_parallel(None, _models(), workers=2, cache_dir=cached_markets, performance_path=None,
                       progress=lambda phase, fraction, message: phases.append(message),
                       compute_device="cuda")

    assert run["errors"] == {}
    expected_gpu_slots = min(len(devices), 2)
    assert any(message.startswith(
        f"0 of 4 models done on {2 + expected_gpu_slots} workers "
        f"(2 CPU-capacity + {expected_gpu_slots} GPU-added)")
               for message in phases)
    assert all(result["compute_device"] == "cuda" for result in run["results"])


def test_light_results_leave_out_the_charted_series(cached_markets):
    run = run_parallel(None, _models()[:2], workers=2, cache_dir=cached_markets, performance_path=None,
                       keep_heavy=False)

    assert all("series" not in result and "total_return" in result for result in run["results"])


def test_a_merge_that_fails_keeps_the_shards_for_a_later_gather(cached_markets, monkeypatch):
    log = cached_markets / "model_performance.csv"

    def locked(*args, **kwargs):
        raise PermissionError("open in Excel")

    monkeypatch.setattr("performance_log._merge_rows", locked)
    run = run_parallel(None, _models()[:2], workers=2, cache_dir=cached_markets, performance_path=log)

    assert "2 result(s) not merged (open in Excel)" in run["logged"]
    assert len(list((cached_markets / "performance_shards").rglob("*.csv"))) == 2
    monkeypatch.undo()

    from performance_log import gather_shards
    assert sorted(gather_shards(cached_markets / "performance_shards", log)["added"]) == ["btc_cci", "btc_ema"]
    assert not list((cached_markets / "performance_shards").rglob("*.csv"))

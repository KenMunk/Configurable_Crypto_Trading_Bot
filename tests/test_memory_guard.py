"""The memory guard: a run's memory budget, kept by writing chart data to disk and running fewer tasks at once."""

import numpy as np
import pytest

import memory_guard
from memory_guard import MemoryGuard, SpilledSeries, available_memory, clean_spills, spill_series
from tests.test_parallel_backtest import _models, cached_markets  # noqa: F401  (a fixture)


def _guard(tmp_path, *free, percent=80, baseline=1000):
    """A guard over `baseline` bytes free at launch that reads `free` bytes free, one value per check (the last
    one from then on), spilling under `tmp_path`."""
    readings = iter(free)
    last = [free[-1]]

    def measure():
        last[0] = next(readings, last[0])
        return last[0]
    return MemoryGuard(percent, baseline=baseline, measure=measure, spill_root=tmp_path / "spill")


def _result(length):
    series = {"time": list(range(length)), "close": np.arange(length, dtype=float),
              "equity": [10.0 * i for i in range(length)], "indicators": {"ema": np.arange(length) / 2}}
    return {"series": series, "total_return": 1.5}


def test_free_memory_is_read_from_the_operating_system():
    free = available_memory()
    assert isinstance(free, int) and free > 0


def test_spilled_series_read_back_exactly_as_they_were(tmp_path):
    result = _result(50)
    original = {key: (dict(value) if key == "indicators" else value) for key, value in result["series"].items()}

    assert spill_series(result, tmp_path, 0)

    series = result["series"]
    assert isinstance(series, SpilledSeries) and (tmp_path / "0000000.pkl").exists()
    assert series["time"] == original["time"] and isinstance(series["time"], list)  # a list stays a list
    assert np.array_equal(series["close"], original["close"]) and isinstance(series["close"], np.ndarray)
    assert np.array_equal(series["indicators"]["ema"], original["indicators"]["ema"])
    assert sorted(series) == sorted(original) and series.get("missing") is None
    assert not spill_series(result, tmp_path, 1)  # already on disk


def test_only_the_most_recently_read_spilled_series_stay_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_guard, "SPILL_CACHE", 2)
    results = [_result(5) for _ in range(3)]
    for number, result in enumerate(results):
        spill_series(result, tmp_path, number)
    for result in results:
        result["series"]["time"]

    assert list(memory_guard._loaded) == [results[1]["series"].path, results[2]["series"].path]


def test_past_the_spill_threshold_chart_data_goes_to_disk_the_earlier_results_too(tmp_path):
    earlier, new = _result(20), _result(20)
    guard = _guard(tmp_path, 500, 500)  # 500 of an 800 budget: past SPILL_AT

    guard.admit([new], [earlier])

    assert isinstance(earlier["series"], SpilledSeries) and isinstance(new["series"], SpilledSeries)
    assert guard.spilled == 2 and len(list(guard.spill_folder.glob("*.pkl"))) == 2
    assert earlier["total_return"] == 1.5 and earlier["series"]["equity"][-1] == 190.0
    assert "chart data of 2 results kept on disk" in guard.describe()


def test_under_the_spill_threshold_everything_stays_in_memory(tmp_path):
    result = _result(20)
    guard = _guard(tmp_path, 700)  # 300 of 800

    guard.admit([result], [])

    assert isinstance(result["series"], dict) and not guard.spill_folder.exists() and guard.describe() == ""


@pytest.mark.parametrize("free, running, allowed", [
    (700, 0, 8),   # 300 of 800: every worker
    (330, 0, 4),   # 670 of 800 (over 80%): half
    (250, 0, 2),   # 750 of 800 (over 90%): a quarter
    (150, 0, 1),   # over the budget: one at a time
    (150, 1, 0),   # ... and none more while one runs
    (330, 3, 1),   # half of 8 with 3 running: one more
])
def test_fewer_tasks_run_at_once_as_memory_fills(tmp_path, free, running, allowed):
    assert _guard(tmp_path, free).slots(8, running) == allowed


def test_a_throttled_run_says_how_far_it_slowed(tmp_path):
    guard = _guard(tmp_path, 330, 150)
    guard.slots(8)
    guard.slots(8)

    assert guard.fewest_workers == 1 and "slowed to 1 task at a time" in guard.describe()


def test_without_a_memory_reading_the_guard_never_steps_in(tmp_path):
    guard = MemoryGuard(80, baseline=None, measure=lambda: None, spill_root=tmp_path)
    result = _result(3)

    guard.admit([result], [])

    assert guard.budget is None and guard.slots(6) == 6 and isinstance(result["series"], dict)


@pytest.mark.parametrize("bad", [0, -5, 101])
def test_the_limit_must_be_a_percentage(bad):
    with pytest.raises(ValueError, match="percentage"):
        MemoryGuard(bad, baseline=1000, measure=lambda: 1000)


def test_another_open_simulators_spill_folder_is_left_alone(tmp_path):
    import os
    import subprocess
    import sys
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    mine, other, gone = (tmp_path / f"20260101_000000_{pid}_1" for pid in (os.getpid(), os.getppid(),
                                                                           int(ended.stdout)))
    for folder in (mine, other, gone):
        spill_series(_result(3), folder, 0)

    clean_spills(root=tmp_path)

    assert other.exists() and not mine.exists() and not gone.exists()


def test_earlier_runs_spill_folders_are_deleted_and_the_shown_runs_kept(tmp_path):
    old, shown = tmp_path / "old_run", tmp_path / "shown_run"
    spill_series(_result(3), old, 0)
    spill_series(_result(3), shown, 0)

    clean_spills(keep=[shown], root=tmp_path)

    assert not old.exists() and (shown / "0000000.pkl").exists()


def test_the_simulator_reads_the_memory_limit_box():
    from simulator import parse_memory_percent
    assert parse_memory_percent("80") == 80 and parse_memory_percent(" 65% ") == 65
    for bad in ("", "abc", "0", "150"):
        with pytest.raises(ValueError, match="memory limit"):
            parse_memory_percent(bad)


# --- runs under a full budget: slower, on disk, complete -------------------------------------------------

def _stress_inputs():
    import stress_test
    from friction import FrictionModel
    from tests.test_stress_test import SCENARIOS, _daily, _profile
    profiles = [("fast", _profile(period_granularity="ONE_DAY")), ("slow", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=2, max_paths=2)
    return profiles, frictions, {("BTC-USD", "ONE_DAY"): _daily()}, combos


@pytest.mark.parametrize("workers", [1, 3])
def test_a_stress_test_at_the_memory_limit_still_runs_every_task_with_every_chart_point(tmp_path, workers):
    import stress_test
    profiles, frictions, markets, combos = _stress_inputs()
    full_records, _, full_results = stress_test.run_stress(profiles, frictions, markets, combos)
    guard = _guard(tmp_path, 100)  # the budget is used up throughout

    records, _, results = stress_test.run_stress(profiles, frictions, markets, combos, workers=workers,
                                                 compute_device="cpu", memory_guard=guard)

    assert [(r["scenario"], r["mode"], r["return"]) for r in records] == [
        (r["scenario"], r["mode"], r["return"]) for r in full_records]
    assert all(isinstance(result["series"], SpilledSeries) for result in results)
    assert [list(result["series"]["equity"]) for result in results] == [
        list(result["series"]["equity"]) for result in full_results]  # nothing thinned
    if workers > 1:
        assert guard.fewest_workers == 1  # one task at a time at the budget


def test_a_parallel_history_run_at_the_memory_limit_runs_every_model(tmp_path, cached_markets):  # noqa: F811
    from parallel_backtest import run_parallel
    guard = _guard(tmp_path, 100)

    run = run_parallel(None, _models(), workers=2, cache_dir=cached_markets, performance_path=None,
                       compute_device="cpu", memory_guard=guard)
    free = run_parallel(None, _models(), workers=2, cache_dir=cached_markets, performance_path=None,
                        compute_device="cpu")

    assert [r["total_return"] for r in run["results"]] == [r["total_return"] for r in free["results"]]
    assert all(isinstance(r["series"], SpilledSeries) for r in run["results"]) and guard.fewest_workers == 1


def test_the_simulator_finishes_the_run_and_reports_the_guard(tmp_path, monkeypatch):
    from tests.test_synthetic import _run_worker
    from synthetic import normalize
    import simulator
    monkeypatch.setattr(simulator.SimulationWindow, "memory_guard", _guard(tmp_path, 100), raising=False)
    scenarios = [normalize({"name": "crash", "type": "crash", "drop": 0.3, "days": 0.01, "recovery_days": 0.01})]

    events = _run_worker(tmp_path, monkeypatch, "historic+synthetic", scenarios)

    kind, results, logged = events[-1]
    assert kind == "done" and "kept on disk to stay under the memory limit" in logged
    assert sorted(result["model_name"] for result in results) == ["m1", "m1 | crash", "m2", "m2 | crash"]


def test_a_chart_draws_from_spilled_chart_data(tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from backtest import run_backtest
    from chart import BacktestChart
    from tests.test_backtest import _candle, _profile
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    result = run_backtest([_candle(i * 900, p) for i, p in enumerate(prices)], _profile())
    spill_series(result, tmp_path, 0)
    figure = Figure(figsize=(8, 4), dpi=100)
    chart = BacktestChart(figure, FigureCanvasAgg(figure))

    chart.show(result)
    figure.canvas.draw()

    assert list(chart.closes) == prices and "Close  102.00" in chart.tooltip_text(7)

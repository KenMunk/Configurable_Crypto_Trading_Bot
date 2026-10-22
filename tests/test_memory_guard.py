"""The memory guard: a run's memory budget, and how stress, scenario and history runs keep to it."""

import pytest

import memory_guard
from memory_guard import MemoryGuard, available_memory, thin_series
from tests.test_parallel_backtest import _models, cached_markets  # noqa: F401  (a fixture)


def _guard(*free, percent=80, baseline=1000):
    """A guard over `baseline` bytes free at launch that reads `free` bytes free, one value per check."""
    readings = iter(free)
    last = [free[-1]]

    def measure():
        last[0] = next(readings, last[0])
        return last[0]
    return MemoryGuard(percent, baseline=baseline, measure=measure)


def _result(length):
    series = {"time": list(range(length)), "close": [float(i) for i in range(length)],
              "equity": [10.0 * i for i in range(length)], "indicators": {"ema": [i / 2 for i in range(length)]}}
    return {"series": series, "total_return": 1.5}


def test_free_memory_is_read_from_the_operating_system():
    free = available_memory()
    assert isinstance(free, int) and free > 0


def test_the_budget_is_a_share_of_the_memory_free_at_launch():
    guard = _guard(500, 390, 200)  # 500, 610 and 800 bytes used of an 800-byte budget

    assert guard.budget == 800
    assert [guard.check() for _ in range(3)] == ["ok", "thin", "stop"]
    assert guard.peak == 800


def test_past_three_quarters_of_the_budget_every_result_keeps_fewer_chart_points(monkeypatch):
    monkeypatch.setattr(memory_guard, "THIN_POINTS", 5)
    earlier, new = _result(50), _result(50)
    guard = _guard(390)  # 610 of 800: past THIN_AT

    assert guard.admit([new], [earlier])

    for result in (earlier, new):
        series = result["series"]
        assert len(series["time"]) == len(series["close"]) == len(series["indicators"]["ema"]) == 5
        assert series["time"][0] == 0 and series["time"][-1] == 49  # evenly spaced, the last kept
        assert series["equity"] == [10.0 * t for t in series["time"]]  # still aligned with the times
        assert result["total_return"] == 1.5 and result["series_thinned"]
    assert "charts kept 5 points" in guard.describe()


def test_short_series_and_results_under_the_threshold_are_left_alone():
    result = _result(10)
    guard = _guard(900)

    assert guard.admit([result], []) and not guard.thinned
    assert len(thin_series(_result(10), 20)["series"]["time"]) == 10
    assert guard.describe() == ""


def test_at_the_budget_the_run_must_stop_and_says_why():
    guard = _guard(150)  # 850 of 800

    assert not guard.admit([_result(3)], [])
    assert guard.stopped.startswith("stopped at the memory limit")
    assert "80% of the" in guard.stopped and guard.stopped in guard.describe()


def test_without_a_memory_reading_the_guard_never_steps_in():
    guard = MemoryGuard(80, baseline=None, measure=lambda: None)

    assert guard.budget is None and guard.check() == "ok" and guard.admit([_result(3)], [])


@pytest.mark.parametrize("bad", [0, -5, 101])
def test_the_limit_must_be_a_percentage(bad):
    with pytest.raises(ValueError, match="percentage"):
        MemoryGuard(bad, baseline=1000, measure=lambda: 1000)


def test_the_simulator_reads_the_memory_limit_box():
    from simulator import parse_memory_percent
    assert parse_memory_percent("80") == 80 and parse_memory_percent(" 65% ") == 65
    for bad in ("", "abc", "0", "150"):
        with pytest.raises(ValueError, match="memory limit"):
            parse_memory_percent(bad)


# --- runs keeping to the budget --------------------------------------------------------------------------

def _stress_inputs():
    import stress_test
    from friction import FrictionModel
    from tests.test_stress_test import SCENARIOS, _daily, _profile
    profiles = [("fast", _profile(period_granularity="ONE_DAY")), ("slow", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=2, max_paths=2)
    return profiles, frictions, {("BTC-USD", "ONE_DAY"): _daily()}, combos


@pytest.mark.parametrize("workers", [1, 2])
def test_a_stress_test_stops_starting_tasks_at_the_memory_limit(workers):
    import stress_test
    profiles, frictions, markets, combos = _stress_inputs()
    full, _, _ = stress_test.run_stress(profiles, frictions, markets, combos)
    guard = _guard(900, 900, 100)  # the third finished task finds the budget used up

    records, _, results = stress_test.run_stress(profiles, frictions, markets, combos, workers=workers,
                                                 compute_device="cpu", memory_guard=guard)

    assert guard.stopped and 0 < len(results) < len(full) + 2  # stopped early; what finished is kept
    assert len(records) < len(full)


def test_a_parallel_history_run_cancels_the_models_not_started_at_the_memory_limit(cached_markets):  # noqa: F811
    from parallel_backtest import run_parallel
    guard = _guard(100)  # the first model to finish finds the budget used up
    # Enough models that most are still queued when the first finishes (a pool hands a worker a task early).
    models = [(f"{name}_{copy}", profile) for copy in range(4) for name, profile in _models()]

    run = run_parallel(None, models, workers=1, cache_dir=cached_markets, performance_path=None,
                       compute_device="cpu", memory_guard=guard)

    finished = [result for result in run["results"] if result is not None]
    assert guard.stopped and 1 <= len(finished) < len(models) and not run["errors"]


def test_the_simulator_reports_when_the_guard_stepped_in(tmp_path, monkeypatch):
    from tests.test_synthetic import _run_worker
    from synthetic import normalize
    import simulator
    monkeypatch.setattr(simulator.SimulationWindow, "memory_guard", _guard(100), raising=False)
    scenarios = [normalize({"name": "crash", "type": "crash", "drop": 0.3, "days": 0.01, "recovery_days": 0.01})]

    events = _run_worker(tmp_path, monkeypatch, "historic+synthetic", scenarios)

    kind, results, logged = events[-1]
    assert kind == "done" and "stopped at the memory limit" in logged
    assert [result["model_name"] for result in results] == ["m1"]  # the scenarios and the second model skipped

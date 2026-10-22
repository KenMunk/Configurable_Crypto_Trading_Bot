import csv
import math
import re
import queue
from types import SimpleNamespace

import pytest

import stress_test
import synthetic
from friction import FrictionModel
from synthetic import normalize
from tests.test_backtest import FakeCandleClient, _profile
from tests.test_indicators import _candles

DAY = 86_400


def _daily(count=600, seed=7):
    return [{**candle, "start": index * DAY} for index, candle in enumerate(_candles(count, seed))]


def _volatile_daily(count=600, seed=7, daily=0.04):
    """Daily candles moving about as much as crypto does (4% a day)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, daily, count)))
    return [{"start": i * DAY, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1.0}
            for i, c in enumerate(close)]


SCENARIOS = [normalize({"name": "crash", "type": "crash", "drop": 0.4, "days": 3, "recovery_days": 20}),
             normalize({"name": "chop", "type": "sideways", "range": 0.1, "days": 30}),
             normalize({"name": "then rally", "type": "pump", "chain": True, "rise": 0.5, "days": 30, "drop": 0.1,
                        "collapse_days": 5}),
             normalize({"name": "paths", "type": "resample", "days": 30, "paths": 6, "block_days": 3})]


def test_there_are_at_least_three_nightmares_and_they_are_severe(tmp_path):
    nightmares = stress_test.load_nightmares(tmp_path / "nightmares.json")
    runs = synthetic.runs(nightmares)

    assert len(runs) >= 3
    assert all(run[0]["name"].startswith("Nightmare") for run in runs)
    history = _volatile_daily()
    for run in runs:
        built, start = synthetic.build(history, run, DAY)
        closes = [c["close"] for c in built if c["start"] >= start]
        worst = min(closes) / history[-1]["close"] - 1
        swing = max(closes) / min(closes)
        assert worst <= -0.3 or swing >= 1.5, synthetic.run_label(run)  # a deep loss or violent swings


def test_combinations_cover_every_run_alone_plus_chained_pairs():
    nightmares = [normalize({"name": "Nightmare: wipeout", "type": "bear", "drop": 0.9, "days": 30})]

    combos = stress_test.combinations(SCENARIOS, nightmares, pairs=5, max_paths=2, seed=3)

    labels = [label for label, *_ in combos]
    assert labels[:4] == ["crash", "chop -> then rally", "paths", "Nightmare: wipeout"]
    assert [nightmare for _, _, nightmare, _ in combos[:4]] == [False, False, False, True]
    assert combos[2][3] == 2  # resampled paths capped at max_paths
    pairs = combos[4:]
    assert len(pairs) == 5 and all(" -> " in label for label, *_ in pairs)
    for label, run, nightmare, _ in pairs:
        assert len(synthetic.runs(run)) == 1  # the second run is chained on: one continuous path
        assert nightmare == ("Nightmare" in label)
    assert stress_test.combinations(SCENARIOS, nightmares, pairs=5, max_paths=2, seed=3) == combos  # seeded


def test_every_model_runs_every_combination_alone_and_with_history_and_reports_are_written(tmp_path):
    profiles = [("fast", _profile(period_granularity="ONE_DAY")),
                ("slow", _profile(period_granularity="ONE_DAY", indicators={
                    "fast_ema": {"type": "EMA", "period": 5}, "slow_ema": {"type": "EMA", "period": 20},
                    "emergency_ema": {"type": "EMA", "period": 1}}))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=2, max_paths=2)
    progress = []

    records, baselines, results = stress_test.run_stress(
        profiles, frictions, {("BTC-USD", "ONE_DAY"): _daily()}, combos,
        progress=lambda fraction, message: progress.append(fraction))

    runs_per_model = sum(paths for *_, paths in combos) * 2
    assert len(records) == 2 * runs_per_model and set(baselines) == {"fast", "slow"}
    assert {record["mode"] for record in records} == {"alone", "with history"}
    alone = next(r for r in records if r["mode"] == "alone")
    with_history = next(r for r in records if r["mode"] == "with history" and r["scenario"] == alone["scenario"]
                        and r["model"] == alone["model"])
    assert len(with_history["equity"]) > len(alone["equity"])  # with history covers the whole past too
    assert progress[-1] == pytest.approx(1.0)
    assert len(results) == len(records) + 2

    index = stress_test.write_reports(records, baselines, dict(profiles), "test", folder=tmp_path / "report")

    page = index.read_text(encoding="utf-8")
    assert "fast.html" in page and "slow.html" in page and "Verdict" in page
    model_page = (tmp_path / "report" / "fast.html").read_text(encoding="utf-8")
    assert "Scenarios alone" in model_page and "Scenarios with history" in model_page
    assert model_page.count("data:image/png;base64,") == 3  # worst runs, and a chart for each mode
    assert "report.pdf" in page
    pdf = (tmp_path / "report" / "report.pdf").read_bytes()
    pages = len(re.findall(rb"/Type /Page[^s]", pdf))
    per_mode = {(record["model"], record["mode"]): 0 for record in records}
    for record in records:
        per_mode[(record["model"], record["mode"])] += 1
    expected = 1 + 2 + sum(math.ceil(n / stress_test.PDF_BAR_ROWS) + math.ceil(n / stress_test.PDF_TABLE_ROWS)
                           for n in per_mode.values())  # summary, a first page per model, each mode's charts and runs
    assert pdf.startswith(b"%PDF") and pages == expected
    with (tmp_path / "report" / "results.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(records) + 2


def test_worker_processes_give_the_same_records_in_the_same_order():
    profiles = [("fast", _profile(period_granularity="ONE_DAY")),
                ("slow", _profile(period_granularity="ONE_DAY", indicators={
                    "fast_ema": {"type": "EMA", "period": 5}, "slow_ema": {"type": "EMA", "period": 20},
                    "emergency_ema": {"type": "EMA", "period": 1}}))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=2, max_paths=2)
    markets = {("BTC-USD", "ONE_DAY"): _daily()}
    progress = []

    serial = stress_test.run_stress(profiles, frictions, markets, combos)
    parallel = stress_test.run_stress(profiles, frictions, markets, combos, workers=2, compute_device="cpu",
                                      progress=lambda fraction, message: progress.append(message))

    key = lambda record: (record["model"], record["scenario"], record["mode"], record["return"])  # noqa: E731
    assert [key(r) for r in parallel[0]] == [key(r) for r in serial[0]]
    assert {name: key(r) for name, r in parallel[1].items()} == {name: key(r) for name, r in serial[1].items()}
    assert [r["model_name"] for r in parallel[2]] == [r["model_name"] for r in serial[2]]
    assert "on 2 workers" in progress[-1]
    assert all(len(r["series"]["equity"]) <= stress_test.CHART_POINTS for r in parallel[2][2:])


def test_each_model_on_each_scenario_path_is_its_own_task():
    profiles = [("fast", _profile(period_granularity="ONE_DAY")), ("slow", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=1, max_paths=2)
    tasks = stress_test._tasks(profiles, frictions, combos, stress_test.MODES, False)

    baselines = [args[1] for function, args, _ in tasks if function is stress_test._baseline_task]
    runs = [(args[1], args[4], args[6]) for function, args, _ in tasks if function is stress_test._combo_task]
    assert baselines == ["fast", "slow"]  # the real history: one task per model
    assert len(runs) == len(set(runs)) == 2 * sum(paths for *_, paths in combos)  # per model per path
    assert all(count == 2 for function, _, count in tasks if function is stress_test._combo_task)  # both modes


def test_free_workers_take_the_least_busy_models_task_and_share_a_model_when_others_run_out():
    import collections
    queues, running = {"a": [0, 2, 4, 5], "b": [1, 3], "c": []}, collections.Counter()
    assert stress_test._next_tasks(queues, running, free=2) == [0, 1]  # one model per worker while both have work
    running["a"] -= 1  # a's task finished
    assert stress_test._next_tasks(queues, running, free=1) == [2]  # a has fewer running than b
    running["b"] -= 1
    assert stress_test._next_tasks(queues, running, free=1) == [3]  # now b has fewer running
    assert stress_test._next_tasks(queues, running, free=5) == [4, 5]  # only a is left: it takes the idle workers
    assert stress_test._next_tasks(queues, running, free=5) == []  # nothing left to start


def test_one_model_runs_on_every_worker_with_the_same_results(monkeypatch):
    profiles = [("fast", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=2, max_paths=2)
    markets = {("BTC-USD", "ONE_DAY"): _daily()}
    clock = iter(range(0, 10 ** 6))  # every update passes the quarter-second throttle
    monkeypatch.setattr(stress_test.time, "monotonic", lambda: next(clock))
    seen, progress = [], []

    parallel = stress_test.run_stress(profiles, frictions, markets, combos, workers=3, compute_device="cpu",
                                      worker_status=seen.append,
                                      progress=lambda fraction, message: progress.append(message))
    serial = stress_test.run_stress(profiles, frictions, markets, combos)

    assert "on 3 workers" in progress[-1]  # one model, but its tasks share all three workers
    key = lambda record: (record["scenario"], record["mode"], record["path"], record["return"])  # noqa: E731
    assert [key(r) for r in parallel[0]] == [key(r) for r in serial[0]]  # same results, same order


def test_synthetic_scenarios_run_a_task_per_model_per_path_on_workers():
    profiles = [("fast", _profile(period_granularity="ONE_DAY")), ("slow", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    markets = {("BTC-USD", "ONE_DAY"): _daily()}
    scenarios = SCENARIOS[:2] + [{"name": "Too early", "type": "crash", "placement": "at", "at": "1990-01-01",
                                  "drop": 0.3, "days": 5}]

    serial, skipped = stress_test.run_scenarios(profiles, frictions, markets, scenarios)
    parallel, _ = stress_test.run_scenarios(profiles, frictions, markets, scenarios, workers=2, compute_device="cpu")

    paths = sum(synthetic.run_paths(run) for run in synthetic.runs(SCENARIOS[:2]))
    assert len(serial) == 2 * paths
    assert [r["model_name"] for r in parallel] == [r["model_name"] for r in serial]
    assert [r["total_return"] for r in parallel] == [r["total_return"] for r in serial]
    assert serial[0]["model_name"].startswith("fast | ") and "(alone)" not in serial[0]["model_name"]
    assert {r["base_model"] for r in serial} == {"fast", "slow"} and "scenario_path" in serial[0]
    assert len(skipped) == 1 and skipped[0].startswith("Too early: ")


def test_workers_report_what_they_are_running(monkeypatch):
    profiles = [("fast", _profile(period_granularity="ONE_DAY"))]
    frictions = {name: FrictionModel(profile) for name, profile in profiles}
    combos = stress_test.combinations(SCENARIOS, [], pairs=1, max_paths=1)
    markets = {("BTC-USD", "ONE_DAY"): _daily()}
    clock = iter(range(0, 10 ** 6))  # every update passes the quarter-second throttle
    monkeypatch.setattr(stress_test.time, "monotonic", lambda: next(clock))
    seen = []
    stress_test.run_stress(profiles, frictions, markets, combos, worker_status=seen.append)
    running = [row["doing"] for update in seen for row in update if row["doing"]]
    assert {row["worker"] for update in seen for row in update} == {1}
    assert "fast: real history" in running and any(d.startswith("fast: building") for d in running)
    assert any(d.startswith("fast: ") and "(alone)" in d for d in running)
    assert any(0 < row["fraction"] < 1 for update in seen for row in update)  # partway through a run
    assert seen[-1][0]["doing"] is None  # idle at the end

    seen = []  # worker processes: the updates come through a queue, a row per worker
    stress_test.run_stress(profiles, frictions, markets, combos, workers=2, compute_device="cpu",
                           worker_status=seen.append)
    assert seen and {row["worker"] for row in seen[-1]} <= {1, 2}
    assert all(row["doing"] is None for row in seen[-1])


def test_verdicts_name_fragile_weak_robust_and_nightmare_outcomes():
    def record(value, nightmare=False):
        return {"return": value, "buy_and_hold": 0.0, "lost_money": value < 0, "beat_buy_and_hold": value > 0,
                "max_drawdown": max(-value, 0.05), "nightmare": nightmare, "scenario": "s", "mode": "alone"}

    fragile = stress_test.summarize([record(0.2), record(-0.6, True)])
    robust = stress_test.summarize([record(0.1), record(-0.1, True), record(0.3)])
    weak = stress_test.summarize([record(-0.3), record(-0.25), record(0.1)])

    assert stress_test.verdict(fragile).startswith("Fragile: it lost 60%")
    assert "lost half or more in 1" in stress_test.verdict(fragile)
    assert stress_test.verdict(robust).startswith("Robust: it never lost more than 10%")
    assert stress_test.verdict(weak).startswith("Weak: it lost money in 67% of runs")


def test_the_simulator_runs_a_stress_test_and_logs_nothing(tmp_path, monkeypatch):
    from backtest import load_candles
    from simulator import SimulationWindow
    monkeypatch.setattr("simulator.fetch_friction_inputs", lambda client, profiles: {})
    monkeypatch.setattr("simulator.load_candles", lambda client, product_id, granularity, start, end, progress=None:
                        load_candles(client, product_id, granularity, end - 600 * 60, end, cache_dir=tmp_path,
                                     progress=progress))
    monkeypatch.setattr(stress_test, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(stress_test, "NIGHTMARE_SET", tmp_path / "nightmares.json")
    monkeypatch.setattr(stress_test, "NIGHTMARES", [{"name": "Nightmare: drop", "type": "bear", "drop": 0.5,
                                                     "days": 0.02}])
    monkeypatch.setattr(stress_test, "PLAN_FILE", tmp_path / "stress_plan.json")
    stress_test.save_plan([{"name": "quick drop", "modules": [{"name": "drop", "type": "bear", "drop": 0.3,
                                                               "days": 0.01}]}])
    window = SimulationWindow.__new__(SimulationWindow)
    window.progress_queue = queue.Queue()
    window.performance_log_path = tmp_path / "model_performance.csv"
    window.test_options = {"mode": "stress", "scenario_set": "x", "stress_pairs": 1, "stress_paths": 1}
    window.auth_engine = SimpleNamespace(authenticate_coinbase=lambda client=None, **_: {
        "authenticated": True, "account_count": 1, "message": "ok"})
    scenarios = [normalize({"name": "crash", "type": "crash", "drop": 0.3, "days": 0.01, "recovery_days": 0.01})]

    window._simulation_worker(FakeCandleClient(), [("m1", _profile(period_granularity="ONE_MINUTE"))],
                              mode="stress", scenarios=scenarios)

    events = []
    while not window.progress_queue.empty():
        events.append(window.progress_queue.get())
    report = next(event[1] for event in events if event[0] == "report")
    kind, results, logged = events[-1]
    assert kind == "done" and report.exists() and "stress test of" in logged
    assert all(result["stress_test"] for result in results)
    assert not (tmp_path / "model_performance.csv").exists()



# --- the stress plan ---------------------------------------------------------------------------------

def test_the_default_plan_is_written_once_and_an_edited_plan_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(stress_test, "PLAN_FILE", tmp_path / "stress_plan.json")

    first = stress_test.load_plan()

    assert (tmp_path / "stress_plan.json").exists()
    assert first == stress_test.default_plan() == stress_test.load_plan()  # the same baseline every run
    assert len(first) == len(synthetic.runs([normalize(s) for s in synthetic.DEFAULT_SCENARIOS])) + len(
        synthetic.runs([normalize(s) for s in stress_test.NIGHTMARES])) + stress_test.DEFAULT_PAIRS
    assert sum(c["nightmare"] for c in first) >= 3

    edited = first[:2] + [{"name": "my chain", "modules": [{"name": "a", "type": "bear"},
                                                            {"name": "b", "type": "pump"}]}]
    stress_test.save_plan(edited)

    kept = stress_test.load_plan()
    assert [c["name"] for c in kept] == [first[0]["name"], first[1]["name"], "my chain"]
    assert [m["chain"] for m in kept[2]["modules"]] == [False, True]  # one continuous run
    assert stress_test.plan_combinations(kept, max_paths=2)[2][0] == "my chain"


def test_a_plan_is_independent_of_later_scenario_set_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(stress_test, "PLAN_FILE", tmp_path / "stress_plan.json")
    monkeypatch.setattr(synthetic, "DEFAULT_SCENARIOS", [{"name": "changed", "type": "bear", "drop": 0.1}])
    stress_test.save_plan([{"name": "kept", "modules": [{"name": "crash", "type": "crash", "drop": 0.6}]}])

    assert stress_test.load_plan()[0]["modules"][0]["drop"] == 0.6


def test_an_empty_combination_is_refused():
    with pytest.raises(synthetic.ScenarioError, match="no scenario modules"):
        stress_test.normalize_plan([{"name": "empty", "modules": []}])


def test_the_plan_tab_builds_reorders_and_saves_combinations(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from scenario_editor import TestOptionsDialog
    monkeypatch.setattr(stress_test, "PLAN_FILE", tmp_path / "stress_plan.json")
    monkeypatch.setattr(stress_test, "NIGHTMARE_SET", tmp_path / "nightmares.json")
    monkeypatch.setattr(synthetic, "SCENARIO_DIR", tmp_path)
    monkeypatch.setattr(synthetic, "DEFAULT_SET", tmp_path / "set.json")
    monkeypatch.setattr("scenario_editor.save_options", lambda options: None)
    synthetic.save_set([{"name": "crash", "type": "crash"}, {"name": "chop", "type": "sideways"}], tmp_path / "set.json")
    stress_test.save_plan([{"name": "one", "modules": [{"name": "x", "type": "bear"}]},
                           {"name": "two", "modules": [{"name": "y", "type": "bear"}]}])
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.geometry("+-3000+-3000")
    try:
        saved = []
        dialog = TestOptionsDialog(root, {"mode": "stress", "scenario_set": str(tmp_path / "set.json"), "top_n": 5,
                                          "stress_paths": 3}, saved.append)
        assert [c["name"] for c in dialog.plan] == ["one", "two"]

        _, available, add, create = dialog._add_combination()
        for index in (1, 0, 2):  # chop, then crash, then the first nightmare
            available.selection_clear(0, "end")
            available.selection_set(index)
            add()
        create()
        assert dialog.plan[-1]["name"].startswith("chop -> crash -> Nightmare")
        assert dialog.plan[-1]["nightmare"] and dialog.plan[-1]["modules"][1]["chain"]

        rows = {"0": 10, "1": 30, "2": 50}
        dialog.plan_table.identify_row = lambda y: min(rows, key=lambda item: abs(rows[item] - y))
        dialog._plan_drag_start(SimpleNamespace(y=50))
        dialog._plan_drag_motion(SimpleNamespace(y=10))
        dialog._plan_drag_end(SimpleNamespace(y=10))
        assert [c["name"][:4] for c in dialog.plan] == ["chop", "one", "two"]

        dialog.plan_table.selection_set("1")
        dialog._duplicate_combination()
        dialog._save()
        assert saved and [c["name"][:9] for c in stress_test.load_plan()] == ["chop -> c", "one", "one (copy", "two"]
    finally:
        root.destroy()

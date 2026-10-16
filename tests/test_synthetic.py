import math
import queue
from types import SimpleNamespace

import numpy as np
import pytest

import synthetic
from synthetic import ScenarioError, build, load_set, normalize, paths, save_set
from tests.test_backtest import FakeCandleClient, _profile
from tests.test_indicators import FIFTEEN_MINUTES, _candles

DAY = 86_400


def _daily(count=800, seed=7):
    candles = _candles(count, seed)
    return [{**candle, "start": index * DAY} for index, candle in enumerate(candles)]


def _segment(built, start):
    return [candle["close"] for candle in built if candle["start"] >= start]


@pytest.mark.parametrize("scenario, low, end", [
    ({"type": "crash", "drop": 0.5, "days": 7, "recovery_days": 60, "recovery": 0.5}, -0.5, -0.25),
    ({"type": "bear", "drop": 0.7, "days": 200}, -0.7, -0.7),
    ({"type": "pump", "rise": 2.0, "days": 30, "drop": 0.6, "collapse_days": 20}, None, 0.2),
    ({"type": "sideways", "range": 0.15, "days": 120}, None, 0.0),
])
def test_appended_scenarios_follow_their_shape_and_end_where_they_say(scenario, low, end):
    history = _daily()
    built, start = build(history, {"name": "s", "placement": "append", "seed": 3, **scenario}, DAY)

    closes = _segment(built, start)
    begin = history[-1]["close"]
    assert built[:len(history)] == history and start == history[-1]["start"] + DAY
    assert closes[-1] / begin - 1 == pytest.approx(end, abs=0.02)
    if low is not None:
        assert min(closes) / begin - 1 == pytest.approx(low, abs=0.12)


def test_candles_are_consistent_and_continue_on_the_candle_grid():
    built, start = build(_daily(), {"name": "v", "type": "volatility", "days": 60, "volatility": 2.0, "seed": 1}, DAY)

    synthetic_part = [candle for candle in built if candle["start"] >= start]
    assert len(synthetic_part) == 60
    assert all(c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"]
               for c in synthetic_part)
    assert all(b["start"] - a["start"] == DAY for a, b in zip(synthetic_part, synthetic_part[1:]))
    assert all(b["open"] == a["close"] for a, b in zip(synthetic_part, synthetic_part[1:]))


def test_the_same_seed_gives_the_same_candles_and_paths_differ():
    scenario = {"name": "r", "type": "resample", "days": 90, "paths": 3, "block_days": 5, "seed": 4}
    history = _daily()

    first, _ = build(history, scenario, DAY, path=0)
    again, _ = build(history, scenario, DAY, path=0)
    other, _ = build(history, scenario, DAY, path=1)

    assert first == again and first != other
    assert paths(scenario) == 3 and paths({"type": "crash"}) == 1


def test_a_scenario_at_a_date_rewrites_history_from_there_and_shifts_what_follows():
    history = _daily()
    at = "1971-01-01"  # day 365
    built, start = build(history, {"name": "c", "type": "bear", "placement": "at", "at": at, "drop": 0.5,
                                   "days": 30, "seed": 2}, DAY)

    assert start == 365 * DAY and len(built) == len(history)
    assert built[:365] == history[:365]
    assert built[365 + 29]["close"] / history[364]["close"] == pytest.approx(0.5, rel=0.02)
    # Later history keeps its shape from the new level.
    ratio = built[400]["close"] / history[400]["close"]
    assert built[500]["close"] / history[500]["close"] == pytest.approx(ratio)
    with pytest.raises(ScenarioError, match="outside the history"):
        build(history, {"name": "late", "type": "bear", "placement": "at", "at": "2030-01-01"}, DAY)


@pytest.mark.parametrize("scenario, message", [
    ({"type": "meteor"}, "type must be one of"),
    ({"type": "crash", "drop": 1.5}, "drop must be between 0 and 1"),
    ({"type": "bear", "placement": "at", "at": "March"}, "at must be a date"),
    ({"type": "resample", "paths": 0}, "paths must be from 1 to 500"),
    ({"type": "sideways", "days": "long"}, "days must be a number"),
])
def test_scenario_mistakes_are_explained(scenario, message):
    with pytest.raises(ScenarioError, match=message):
        normalize({"name": "x", **scenario})


def test_scenario_sets_round_trip_with_defaults_filled_in(tmp_path):
    path = tmp_path / "set.json"
    save_set([{"name": "quick crash", "type": "crash", "drop": 0.3}], path)

    loaded = load_set(path)

    assert loaded[0]["drop"] == 0.3 and loaded[0]["recovery_days"] == synthetic.PARAMETERS["crash"]["recovery_days"]
    assert {s["type"] for s in synthetic.DEFAULT_SCENARIOS} == set(synthetic.TYPES)  # every type has an example


def test_intraday_scenarios_scale_days_to_candles():
    history = _candles(3000)
    built, start = build(history, {"name": "b", "type": "bear", "drop": 0.2, "days": 2}, FIFTEEN_MINUTES)

    assert len(_segment(built, start)) == 2 * 96


# --- the simulator --------------------------------------------------------------------------

def test_the_scenario_matrix_puts_the_most_robust_model_first():
    from simulator import HISTORIC, scenario_matrix

    def result(model, source, total, held=0.0, base=None):
        return {"model_name": model, "base_model": base or model, "data_source": source, "product_id": "BTC-USD",
                "total_return": total, "buy_and_hold_return": held}

    results = [result("a", HISTORIC, 5.0), result("b", HISTORIC, 2.0),
               result("a | crash", "crash", -0.4, -0.5, "a"), result("b | crash", "crash", -0.1, -0.5, "b"),
               result("a | paths #1", "paths", 0.3, 0.1, "a"), result("a | paths #2", "paths", -0.2, 0.1, "a"),
               result("b | paths #1", "paths", 0.1, 0.1, "b"), result("b | paths #2", "paths", 0.2, 0.1, "b")]

    sources, rows = scenario_matrix(results)

    assert sources == [HISTORIC, "crash", "paths"]
    assert [row["model"] for row in rows] == ["b", "a"]  # b's worst (-10%) beats a's (-40%)
    a = rows[1]
    assert a["cells"]["paths"] == (0.3, -0.2, 2) and a["worst"] == -0.4 and (a["beat"], a["tested"]) == (2, 3)


def test_the_historically_best_models_of_each_currency_are_found(tmp_path):
    import json
    from performance_log import profile_fingerprint
    from simulator import top_by_history
    rows, products, files = {}, {}, []
    for name, product, growth in (("b1", "BTC-USD", 9), ("b2", "BTC-USD", 3), ("b3", "BTC-USD", 6),
                                  ("e1", "ETH-USD", 2), ("e2", "ETH-USD", 4)):
        profile = {"product_id": product, "strategy": {}}
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        rows[name] = {"growth_multiple": str(growth), "profile_fingerprint": profile_fingerprint(profile)}
        products[str(path)] = product
        files.append(str(path))
    rows["b2"]["profile_fingerprint"] = "edited since"

    top = top_by_history(files, products, rows, count=2)

    assert sorted(path.split("\\")[-1].split("/")[-1] for path in top) == ["b1.json", "b3.json", "e1.json",
                                                                           "e2.json"]


def _run_worker(tmp_path, monkeypatch, mode, scenarios):
    from backtest import load_candles
    from simulator import SimulationWindow
    monkeypatch.setattr("simulator.fetch_friction_inputs", lambda client, profiles: {})
    monkeypatch.setattr("simulator.load_candles", lambda client, product_id, granularity, start, end, progress=None:
                        load_candles(client, product_id, granularity, end - 600 * 60, end, cache_dir=tmp_path,
                                     progress=progress))
    window = SimulationWindow.__new__(SimulationWindow)
    window.progress_queue = queue.Queue()
    window.performance_log_path = tmp_path / "model_performance.csv"
    window.auth_engine = SimpleNamespace(authenticate_coinbase=lambda client=None, **_: {
        "authenticated": True, "account_count": 1, "message": "ok"})
    profile = _profile(period_granularity="ONE_MINUTE")
    window._simulation_worker(FakeCandleClient(), [("m1", profile), ("m2", profile)], mode=mode, scenarios=scenarios)
    events = []
    while not window.progress_queue.empty():
        events.append(window.progress_queue.get())
    return events


def test_a_synthetic_only_run_tests_every_model_on_every_path_and_logs_nothing(tmp_path, monkeypatch):
    scenarios = [normalize({"name": "crash", "type": "crash", "drop": 0.3, "days": 0.01, "recovery_days": 0.01}),
                 normalize({"name": "paths", "type": "resample", "days": 0.02, "paths": 2, "block_days": 0.005})]

    events = _run_worker(tmp_path, monkeypatch, "synthetic", scenarios)

    kind, results, logged = events[-1]
    assert kind == "done", events[-1]
    assert sorted(result["model_name"] for result in results) == [
        "m1 | crash", "m1 | paths #1", "m1 | paths #2", "m2 | crash", "m2 | paths #1", "m2 | paths #2"]
    assert {result["data_source"] for result in results} == {"crash", "paths"}
    assert "synthetic results aren't logged" in logged
    assert not (tmp_path / "model_performance.csv").exists()


def test_historic_plus_synthetic_logs_only_the_historic_results(tmp_path, monkeypatch):
    scenarios = [normalize({"name": "bear", "type": "bear", "drop": 0.2, "days": 0.02})]

    events = _run_worker(tmp_path, monkeypatch, "historic+synthetic", scenarios)

    _, results, logged = events[-1]
    assert [result.get("data_source", "Historic") for result in results] == ["Historic", "Historic", "bear", "bear"]
    assert logged.startswith("model_performance.csv: 2 added")
    rows = (tmp_path / "model_performance.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3 and all("|" not in row for row in rows)  # header and the two historic models


# --- realism and chains ------------------------------------------------------------------------

def test_scenario_paths_wander_like_a_market_instead_of_running_straight():
    history = _daily()
    built, start = build(history, {"name": "b", "type": "bear", "drop": 0.6, "days": 300, "seed": 5}, DAY)
    closes = np.log(_segment(built, start))
    straight = np.linspace(closes[0], closes[-1], len(closes))

    assert np.abs(closes - straight).max() > 0.1  # it strays well off a straight line...
    rallies = np.diff(closes) > 0
    assert 0.3 < rallies.mean() < 0.55  # ...with plenty of up days on the way down
    assert closes[-1] - closes[0] == pytest.approx(math.log(0.4), abs=0.03)  # and still ends where it says
    big = np.abs(np.diff(closes)) > 3 * np.std(np.diff(closes))
    assert big.any()  # fat tails: some outsized days


def test_chained_modules_make_one_run_that_continues_from_each_module():
    crash = {"name": "crash", "type": "crash", "drop": 0.4, "days": 5, "recovery_days": 20, "recovery": 0.0, "seed": 1}
    chop = {"name": "chop", "type": "sideways", "chain": True, "range": 0.1, "days": 60, "seed": 2}
    rally = {"name": "rally", "type": "pump", "chain": True, "rise": 1.0, "days": 90, "drop": 0.2,
             "collapse_days": 10, "seed": 3}
    alone = {"name": "alone", "type": "bear", "drop": 0.3, "days": 30, "seed": 4}

    runs = synthetic.runs([normalize(s) for s in (crash, chop, rally, alone)])

    assert [synthetic.run_label(run) for run in runs] == ["crash -> chop -> rally", "alone"]
    history = _daily()
    built, start = build(history, runs[0], DAY)
    closes = _segment(built, start)
    assert len(closes) == 25 + 60 + 100
    begin = history[-1]["close"]
    assert closes[24] / begin == pytest.approx(0.6, rel=0.03)  # the crash's end...
    assert closes[84] / begin == pytest.approx(0.6, rel=0.03)  # ...is where the chop starts and ends
    assert closes[-1] / begin == pytest.approx(0.6 * 2.0 * 0.8, rel=0.03)  # the rally continues from it
    assert synthetic.run_paths(runs[0]) == 1
    assert synthetic.run_paths([normalize({"type": "resample", "paths": 4}), normalize(chop)]) == 4


def test_a_chain_written_into_history_starts_at_its_first_modules_date():
    history = _daily()
    run = [normalize({"name": "a", "type": "bear", "placement": "at", "at": "1971-01-01", "drop": 0.2, "days": 10}),
           normalize({"name": "b", "type": "bear", "chain": True, "drop": 0.2, "days": 10})]

    built, start = build(history, run, DAY)

    assert start == 365 * DAY and len(built) == len(history)
    assert built[365 + 19]["close"] / history[364]["close"] == pytest.approx(0.64, rel=0.03)


# --- the editor ------------------------------------------------------------------------------

tk = pytest.importorskip("tkinter")


@pytest.fixture
def editor(tmp_path, monkeypatch):
    from scenario_editor import TestOptionsDialog
    monkeypatch.setattr(synthetic, "SCENARIO_DIR", tmp_path)
    monkeypatch.setattr(synthetic, "DEFAULT_SET", tmp_path / "stress_test.json")
    save_set([{"name": n, "type": "bear", "seed": i} for i, n in enumerate("abcd")], tmp_path / "stress_test.json")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.geometry("+-3000+-3000")
    saved = []
    dialog = TestOptionsDialog(root, {"mode": "synthetic", "scenario_set": str(tmp_path / "stress_test.json"),
                                      "top_n": 5}, saved.append)
    dialog.window.geometry("+-3000+-3000")
    dialog.saved = saved
    yield dialog
    root.destroy()


def _names(dialog):
    return [scenario["name"] for scenario in dialog.scenarios]


def test_modules_chain_to_the_one_above_and_the_run_column_shows_it(editor):
    editor.table.selection_set("2")
    editor._set_chain(True)
    editor.table.selection_set("3")
    editor._set_chain(True)

    assert [editor.table.set(item, "run") for item in editor.table.get_children()] == [
        "Run 1", "Run 2 \u00b7 step 1 of 3", "Run 2 \u00b7 step 2 of 3", "Run 2 \u00b7 step 3 of 3"]
    editor.table.selection_set("0")
    editor._set_chain(True)  # the first module has nothing above it
    assert not editor.scenarios[0]["chain"] and "first module" in editor.note_var.get()


def test_dragging_a_row_reorders_the_modules(editor):
    rows = {"0": 10, "1": 30, "2": 50, "3": 70}
    editor.table.identify_row = lambda y: min(rows, key=lambda item: abs(rows[item] - y))
    editor._drag_start(SimpleNamespace(y=70))  # pick up "d"
    editor._drag_motion(SimpleNamespace(y=10))  # over "a"
    editor._drag_end(SimpleNamespace(y=10))

    assert _names(editor) == ["d", "a", "b", "c"]
    assert editor.table.selection() == ("0",)


def test_a_duplicate_follows_its_original_and_saving_keeps_the_chain(editor, tmp_path):
    editor.table.selection_set("1")
    editor._set_chain(True)
    editor._duplicate()

    assert _names(editor) == ["a", "b", "b (copy)", "c", "d"]
    assert [s["chain"] for s in editor.scenarios] == [False, True, True, False, False]
    editor._save()
    assert editor.saved and [s["chain"] for s in load_set(tmp_path / "stress_test.json")] == [
        False, True, True, False, False]

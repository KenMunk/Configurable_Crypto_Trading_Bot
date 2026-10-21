"""Choosing which models the charts show: the Choose models dialog and the comparison table."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest import run_backtest
from chart import MODEL_COLORS
from simulator import matching_models
from tests.test_backtest import _candle, _profile

tk = pytest.importorskip("tkinter")
PRICES = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]


def test_matching_models_needs_every_word_and_ignores_case():
    names = ["btc_15m_01", "BTC_balanced_02", "eth_15m_01"]

    assert matching_models(names, "") == [0, 1, 2]
    assert matching_models(names, "btc") == [0, 1]
    assert matching_models(names, " 15M  01 ") == [0, 2]
    assert matching_models(names, "sol") == []


@pytest.fixture
def window(monkeypatch, tmp_path):
    import simulator

    monkeypatch.setattr(simulator, "CONFIG_DIRECTORY", tmp_path / "config")  # never the real model library
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.geometry("+-3000+-3000")  # mapped, so Tk delivers events, but off-screen
    monkeypatch.setattr(simulator, "load_environment_config", lambda: {})
    window = simulator.SimulationWindow(root)
    candles = [_candle(i * 900, price) for i, price in enumerate(PRICES)]
    results = []
    for name, slow in (("alpha", 3), ("beta", 5), ("gamma", 4)):
        profile = _profile()
        profile["indicators"]["slow_ema"]["period"] = slow
        result = run_backtest(candles, profile)
        result["model_name"], result["granularity"] = name, "FIFTEEN_MINUTE"
        results.append(result)
    window.progress_queue.put(("done", results, "model_performance.csv: 3 added"))
    window._poll_progress()
    yield window
    root.destroy()


def _charted(window):
    return [run["model_name"] for run in window.value_chart.runs]


def _rows(window, *names):
    return [item for item in window.comparison_table.get_children()
            if window.comparison_rows[item]["result"]["model_name"] in names]


def test_a_run_charts_every_model(window):
    assert _charted(window) == ["alpha", "beta", "gamma"]
    assert list(window.model_picker["values"]) == ["alpha", "beta", "gamma"]
    assert window.view_var.get() == "Charts show all 3 models"


def test_selected_comparison_rows_become_the_charted_models(window):
    window.comparison_table.selection_set(_rows(window, "alpha", "gamma"))

    window._chart_selected_rows()

    assert _charted(window) == ["alpha", "gamma"]
    assert list(window.model_picker["values"]) == ["alpha", "gamma"]
    assert window.view_var.get() == "Charts show 2 of 3 models"
    assert window.results_tabs.select() == str(window.value_tab)
    top = _rows(window, "alpha", "gamma")[0]  # the selected row highest in the table is on show
    assert window.chart.result is window.comparison_rows[top]["result"]
    hidden = [window.comparison_rows[item]["result"]["model_name"] for item in window.comparison_table.get_children()
              if "hidden" in window.comparison_table.item(item, "tags")]
    assert hidden == ["beta"]


def test_one_selected_row_opens_its_chart(window):
    window.comparison_table.selection_set(_rows(window, "beta"))

    window._chart_selected_rows()

    assert _charted(window) == ["beta"]
    assert window.chart.result["model_name"] == "beta"
    assert window.results_tabs.select() == str(window.chart_tab)


def test_a_model_keeps_its_color_when_others_are_hidden(window):
    window._set_view([2])
    window._set_view([0, 2], current=2)

    gamma = next(line for line in window.value_chart.ax.get_lines() if line.get_label().startswith("gamma"))
    assert gamma.get_color() == MODEL_COLORS[2]


def test_double_clicking_a_hidden_model_adds_it_to_the_charts(window):
    window._set_view([0])
    item = _rows(window, "beta")[0]
    window.comparison_table.identify_row = lambda y: item  # the row under the pointer

    window._open_compared_model(SimpleNamespace(y=0))

    assert _charted(window) == ["alpha", "beta"]
    assert window.chart.result["model_name"] == "beta"


def test_the_choose_dialog_keeps_choices_outside_the_search(window):
    dialog = window._choose_models()
    dialog.geometry("+-3000+-3000")
    dialog.update()
    search, listbox = _find(dialog, tk.Entry, "TEntry"), _find(dialog, tk.Listbox)
    apply = next(button for button in _all(dialog) if isinstance(button, tk.Widget)
                 and button.winfo_class() == "TButton" and button.cget("text") == "Apply")

    search.insert(0, "a")  # alpha, beta, gamma all contain "a"
    listbox.selection_clear(0, "end")
    listbox.selection_set(1)
    listbox.event_generate("<<ListboxSelect>>")
    dialog.update()
    search.delete(0, "end")
    search.insert(0, "gam")
    assert listbox.size() == 1 and not listbox.curselection()  # gamma is listed but not chosen
    listbox.selection_set(0)
    listbox.event_generate("<<ListboxSelect>>")
    dialog.update()
    apply.invoke()

    assert _charted(window) == ["beta", "gamma"]


def test_the_choose_dialog_needs_at_least_one_model(window):
    dialog = window._choose_models()
    clear = next(button for button in _all(dialog) if button.winfo_class() == "TButton"
                 and button.cget("text") == "Clear listed")
    apply = next(button for button in _all(dialog) if button.winfo_class() == "TButton"
                 and button.cget("text") == "Apply")

    clear.invoke()
    apply.invoke()

    assert dialog.winfo_exists() and _charted(window) == ["alpha", "beta", "gamma"]
    dialog.destroy()


def _all(widget):
    for child in widget.winfo_children():
        yield child
        yield from _all(child)


def _find(dialog, kind, widget_class=None):
    return next(child for child in _all(dialog)
                if isinstance(child, kind) or (widget_class and child.winfo_class() == widget_class))


# --- the mode toggle and the time spent -----------------------------------------------

from simulator import compare_timings, describe_mode, format_elapsed  # noqa: E402


def test_format_elapsed():
    assert format_elapsed(8.44) == "8.4 s"
    assert format_elapsed(125.31) == "2 min 05.3 s"
    assert format_elapsed(3725) == "1 h 02 min"


def test_describe_mode():
    assert describe_mode(10, 8) == "parallel, 8 workers"  # never more workers than models
    assert describe_mode(10, 1) == "single-threaded (one model)"
    assert describe_mode(1, 8) == "single-threaded"


def test_compare_timings_needs_both_modes_on_the_same_models():
    single = {"seconds": 10.5, "models": ["a", "b"], "mode": "single-threaded"}
    parallel = {"seconds": 5.25, "models": ["b", "a"], "mode": "parallel, 2 workers"}

    assert compare_timings({"single": single}) == ""
    line = compare_timings({"single": single, "parallel": parallel})
    assert line.startswith("Last single-threaded run: 10.5 s for 2 models; last parallel run (2 workers): 5.2 s")
    assert line.endswith("Parallel was 2.0x as fast, saving 5.2 s")
    slower = {**parallel, "seconds": 21.0}
    assert compare_timings({"single": single, "parallel": slower}).endswith("Single-threaded was 2.0x as fast")
    other = {**parallel, "models": ["c", "d"]}
    assert "different models" in compare_timings({"single": single, "parallel": other})


def test_the_mode_button_toggles_parallel_and_the_worker_box(window):
    assert window.parallel_var.get() and window.mode_text.get() == "Mode: parallel (multi-process)"

    window.mode_button.invoke()

    assert not window.parallel_var.get() and window.mode_text.get() == "Mode: single-threaded"
    assert window.workers_box.instate(["disabled"])
    window.mode_button.invoke()
    assert window.workers_box.instate(["!disabled"])


def test_time_spent_runs_during_a_run_and_compares_the_modes(window, monkeypatch):
    clock = iter([103.0, 110.5, 205.25])  # the times _show_time reads
    monkeypatch.setattr("simulator.time.monotonic", lambda: next(clock))
    names = [result["model_name"] for result in window.results]

    window.run_timing = {"started": 100.0, "mode": "single-threaded", "models": names, "kind": "single"}
    window._show_time()
    assert window.time_var.get() == "3.0 s so far (3 models, single-threaded)"
    window.progress_queue.put(("done", window.results, "logged"))
    window._poll_progress()
    assert window.time_var.get() == "10.5 s for 3 models (single-threaded)"
    assert window.timing_compare_var.get() == "Run the same models in the other mode to compare the times"

    window.run_timing = {"started": 200.0, "mode": "parallel, 3 workers", "models": names, "kind": "parallel"}
    window.progress_queue.put(("done", window.results, "logged"))
    window._poll_progress()
    assert window.time_var.get() == "5.2 s for 3 models (parallel, 3 workers)"
    assert window.timing_compare_var.get().endswith("Parallel was 2.0x as fast, saving 5.2 s")


def test_auto_gives_every_model_its_own_worker(window, monkeypatch):
    """One model, one set of data, one worker: auto never groups models or runs a history run on one thread."""
    started = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            started["args"] = args

        def start(self):
            pass

    monkeypatch.setattr("simulator.threading.Thread", FakeThread)
    monkeypatch.setattr("simulator.max_workers", lambda: 10)
    monkeypatch.setattr("parallel_backtest.max_workers", lambda: 10)
    monkeypatch.setattr(window, "_selected_models", lambda: [(name, {"period_granularity": "ONE_DAY"})
                                                             for name in "abcde"])
    monkeypatch.setattr("simulator.describe_profile", lambda profile: (True, ""))
    monkeypatch.setattr(window.auth_engine, "create_coinbase_client", lambda **kwargs: object())
    monkeypatch.setattr(window.root, "after", lambda *args: None)
    window.parallel_var.set(True)
    window.workers_var.set("auto")
    window.test_options = dict(window.test_options, mode="historic")

    window._run_simulation()

    assert started["args"][4] == 10  # small daily models too: each gets a worker
    assert window.run_timing["mode"] == "parallel, 5 workers"


def test_the_run_passes_the_chosen_data_mode_to_the_worker(window, monkeypatch):
    """The timing label once overwrote the data mode, so a stress test ran as plain history."""
    started = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            started["args"] = args

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr("simulator.threading.Thread", FakeThread)
    monkeypatch.setattr(window, "_selected_models", lambda: [("a", {}), ("b", {})])
    monkeypatch.setattr("simulator.describe_profile", lambda profile: (True, ""))
    monkeypatch.setattr(window.auth_engine, "create_coinbase_client", lambda **kwargs: object())
    monkeypatch.setattr(window.root, "after", lambda *args: None)
    window.parallel_var.set(True)
    window.workers_var.set("2")
    window.test_options = dict(window.test_options, mode="stress")

    window._run_simulation()

    assert started["args"][5] == "stress"
    assert window.run_timing["mode"].startswith("parallel")


def test_the_workers_table_shows_each_worker_during_a_stress_test(window):
    from simulator import format_worker_row
    assert format_worker_row({"worker": 2, "doing": "a: Crash (alone)", "fraction": 0.45}) == (
        "Worker 2", "a: Crash (alone)", "█" * 4 + "░" * 6 + " 45%")
    assert format_worker_row({"worker": 1, "doing": None, "fraction": 0})[1].startswith("idle")
    assert not window.workers_table.winfo_ismapped() and not window.workers_table.grid_info()

    window.progress_queue.put(("workers", [{"worker": 1, "doing": "a: Crash (alone)", "fraction": 0.5},
                                           {"worker": 2, "doing": None, "fraction": 0.0}]))
    window._poll_progress()
    assert window.workers_table.grid_info()
    assert [window.workers_table.item(i, "values")[0] for i in window.workers_table.get_children()] == [
        "Worker 1", "Worker 2"]

    window.progress_queue.put(("workers", [{"worker": 1, "doing": "a: Crash (with history)", "fraction": 1.0},
                                           {"worker": 2, "doing": None, "fraction": 0.0}]))
    window._poll_progress()
    assert window.workers_table.item("worker1", "values")[1] == "a: Crash (with history)"  # updated in place

    window.progress_queue.put(("done", window.results, "logged"))
    window._poll_progress()
    assert not window.workers_table.grid_info()  # hidden when the run ends


def test_the_scenario_charts_tab_charts_each_data_set_on_its_own(window):
    import copy
    results = []
    for result in window.results:  # history, plus two paths of a scenario for each model
        name = result["model_name"]
        results.append(dict(result, base_model=name))
        for path in (0, 1):
            synthetic_run = copy.deepcopy(result)
            synthetic_run.update(model_name=f"{name} | crash #{path + 1}", base_model=name, data_source="crash",
                                 scenario_path=path, scenario_start=result["series"]["time"][5],
                                 total_return=result["total_return"] - 0.1 * path)
            results.append(synthetic_run)
    window.progress_queue.put(("done", results, "synthetic results aren't logged"))
    window._poll_progress()

    tree = window.scenario_chart_tree
    (market,) = tree.get_children()
    assert tree.item(market, "text") == "BTC-USD FIFTEEN_MINUTE"
    historic, crash = tree.get_children(market)
    assert tree.item(historic, "text") == "Historic" and tree.item(crash, "text") == "crash (2 paths)"
    assert str(tree.item(crash, "values")[0]) == "3" and "worst" in tree.item(crash, "values")[1]
    assert [tree.item(item, "text") for item in tree.get_children(crash)] == ["path #1", "path #2"]
    assert "3 data sets (2 synthetic) on 1 market" in window.scenario_chart_note.get()
    window.root.update()
    assert window.scenario_chart.ax.get_title(loc="left") == "Historic \u00b7 BTC-USD FIFTEEN_MINUTE"
    assert len(window.scenario_chart.runs) == 3
    assert len(window.scenario_chart.axes) == 1  # no tax panel: the value chart gets the height

    window._step_scenario_chart(1)
    chart = window.scenario_chart
    assert chart.ax.get_title(loc="left") == "crash #1 \u00b7 BTC-USD FIFTEEN_MINUTE"
    assert {run["model_name"] for run in chart.runs} == {"alpha | crash #1", "beta | crash #1", "gamma | crash #1"}
    assert "Scenario starts" in [text.get_text() for text in chart.ax.get_legend().get_texts()]
    window._step_scenario_chart(5)  # stops at the last
    assert tree.item(tree.selection()[0], "text") == "path #2"
    assert window.scenario_chart.ax.get_title(loc="left") == "crash #2 \u00b7 BTC-USD FIFTEEN_MINUTE"


def test_a_failed_run_reports_its_time_but_is_not_compared(window, monkeypatch):
    clock = iter([4.0])
    monkeypatch.setattr("simulator.time.monotonic", lambda: next(clock))
    window.run_timing = {"started": 0.0, "mode": "single-threaded", "models": ["a"], "kind": "single"}
    monkeypatch.setattr("simulator.messagebox", None)

    window.progress_queue.put(("error", RuntimeError("boom")))
    window._poll_progress()

    assert window.time_var.get() == "4.0 s until the run stopped with an error (single-threaded)"
    assert window.timings == {}


# --- the list beside the Model value chart ---------------------------------------------

from simulator import assign_colors, default_view  # noqa: E402


def test_a_run_of_many_models_opens_on_the_best_eight():
    results = [{"total_return": value} for value in (0.5, 3.0, -0.2, 1.0, 2.0, 0.1, 0.0, 4.0, 0.9, 0.3)]

    assert default_view(results) == [0, 1, 3, 4, 5, 7, 8, 9]  # everything but the two worst
    assert default_view(results, limit=2) == [1, 7]
    assert default_view(results[:3]) == [0, 1, 2]


def test_colors_stay_with_a_model_and_joining_models_take_free_ones():
    slots = assign_colors({}, [3, 5, 9])
    assert slots == {3: 0, 5: 1, 9: 2}

    slots = assign_colors(slots, [5, 9, 12])  # 3 leaves, so 12 takes its color
    assert slots == {5: 1, 9: 2, 12: 0}
    assert sorted(assign_colors(slots, [5, 9, 12, 20, 21]).values()) == [0, 1, 2, 3, 4]


def _listed(window):
    return [window.view_list.get(row).split()[0] for row in range(window.view_list.size())]


def _picked(window):
    return [window.view_list.get(row).split()[0] for row in window.view_list.curselection()]


def test_the_list_beside_the_chart_shows_the_models_best_first(window):
    best_first = [r["model_name"] for r in sorted(window.results, key=lambda r: r["total_return"], reverse=True)]

    assert _listed(window) == best_first
    assert sorted(_picked(window)) == ["alpha", "beta", "gamma"]
    assert window.view_list_note.get() == "3 of 3 on the chart"


def test_clicking_in_the_list_redraws_the_chart_with_those_models(window):
    window.results_tabs.select(window.value_tab)  # Tk delivers events only to widgets on screen
    window.root.update()
    row = _listed(window).index("beta")
    window.view_list.selection_clear(0, "end")
    window.view_list.selection_set(row)
    window.view_list.event_generate("<<ListboxSelect>>")
    window.root.update()
    assert _charted(window) == ["alpha", "beta", "gamma"]  # applied a moment after the last click

    window.root.after(400)
    window.root.update()

    assert _charted(window) == ["beta"]
    assert _picked(window) == ["beta"] and window.view_list_note.get() == "1 of 3 on the chart"
    hidden = [window.comparison_rows[item]["result"]["model_name"] for item in window.comparison_table.get_children()
              if "hidden" in window.comparison_table.item(item, "tags")]
    assert sorted(hidden) == ["alpha", "gamma"]  # the same view as the comparison table and Chart tab


def test_searching_the_list_keeps_models_it_hides_and_the_buttons_act_on_the_listed(window):
    window._set_view([0])  # alpha alone
    window.view_search_var.set("gam")
    assert _listed(window) == ["gamma"] and _picked(window) == []

    window._view_listed(True)
    assert _charted(window) == ["alpha", "gamma"]
    window._view_listed(False)
    assert _charted(window) == ["alpha"]

    window.view_search_var.set("alpha")
    window._view_listed(False)  # would leave nothing on the chart
    assert _charted(window) == ["alpha"] and window.view_list_note.get() == "Choose at least one model"


# --- currency folders and each currency's best model -------------------------------------

def _library_window(monkeypatch, tmp_path):
    """A window over a model library of three BTC models and one ETH model."""
    import simulator
    config = tmp_path / "library"
    for name, product in (("alpha", "BTC-USD"), ("beta", "BTC-USD"), ("gamma", "BTC-USD"), ("delta", "ETH-USD")):
        folder = config / product
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps({**_profile(), "product_id": product}), encoding="utf-8")
    monkeypatch.setattr(simulator, "CONFIG_DIRECTORY", config)
    monkeypatch.setattr(simulator, "load_environment_config", lambda: {})
    root = tk.Tk()
    root.geometry("+-3000+-3000")
    return simulator.SimulationWindow(root), config


def _run_results():
    candles = [_candle(i * 900, price) for i, price in enumerate(PRICES)]
    results = []
    for name, slow, product in (("alpha", 3, "BTC-USD"), ("beta", 5, "BTC-USD"), ("gamma", 4, "BTC-USD"),
                                ("delta", 5, "ETH-USD")):
        profile = _profile(product_id=product)
        profile["indicators"]["slow_ema"]["period"] = slow
        result = run_backtest(candles, profile)
        result["model_name"], result["granularity"] = name, "FIFTEEN_MINUTE"
        results.append(result)
    return results


def _names(window, starred_only=False, which="available"):
    """Model names shown in the Available list, the Selected list, or both."""
    lists = {"available": [window.model_list], "selected": [window.selected_list],
             "both": [window.model_list, window.selected_list]}[which]
    rows = [box.get(i) for box in lists for i in range(box.size())]
    return [row.split()[-2] if row.startswith("\u2605") else row.split()[0]
            for row in rows if not starred_only or row.startswith("\u2605")]


def test_the_model_list_reads_currency_folders_and_filters_by_currency(monkeypatch, tmp_path):
    window, _ = _library_window(monkeypatch, tmp_path)
    try:
        assert list(window.currency_picker["values"]) == ["All currencies", "BTC-USD", "ETH-USD"]
        assert window.model_list.size() + window.selected_list.size() == 4  # one is selected when it opens

        window.currency_var.set("ETH-USD")
        window._fill_model_list()

        assert _names(window) == ["delta"]
    finally:
        window.root.destroy()


def test_after_a_run_each_currencys_best_model_is_recorded_starred_and_selected(monkeypatch, tmp_path):
    from simulator import best_by_currency
    window, config = _library_window(monkeypatch, tmp_path)
    try:
        results = _run_results()
        best = sorted(results[position]["model_name"] for position in best_by_currency(results).values())

        window.progress_queue.put(("done", results, "logged"))
        window._poll_progress()

        recorded = sorted(json.loads((config / product / "best_model.json").read_text(encoding="utf-8"))["model"]
                          for product in ("BTC-USD", "ETH-USD"))
        assert recorded == best
        assert sorted(Path(path).stem for path in window.selected_model_paths()) == best
        assert sorted(_names(window, starred_only=True, which="both")) == best
        assert sorted(_names(window, which="selected")) == best  # moved to the Selected list
        assert sorted(run["model_name"] for run in window.value_chart.runs) == best  # each currency's best
        assert "best per currency: BTC-USD" in window.result_var.get()
    finally:
        window.root.destroy()


def test_a_run_from_a_start_date_records_no_best_and_the_selection_can_be_kept(monkeypatch, tmp_path):
    window, config = _library_window(monkeypatch, tmp_path)
    try:
        window.select_best_var.set(False)
        before = window.selected_model_paths()
        results = _run_results()
        for result in results:
            result["requested_start"] = 1_700_000_000  # not a full-history result

        window.progress_queue.put(("done", results, "logged"))
        window._poll_progress()

        assert not list(config.rglob("best_model.json"))
        assert window.selected_model_paths() == before
    finally:
        window.root.destroy()


def test_each_currencys_best_model_is_selected_when_the_simulator_opens(monkeypatch, tmp_path):
    from model_library import update_best
    config = tmp_path / "library" / "ETH-USD"
    config.mkdir(parents=True)
    (config / "delta.json").write_text(json.dumps({**_profile(), "product_id": "ETH-USD"}), encoding="utf-8")
    update_best("ETH-USD", "delta", config / "delta.json", 1.0, tmp_path / "library")
    window, _ = _library_window(monkeypatch, tmp_path)
    try:
        assert [Path(path).stem for path in window.selected_model_paths()] == ["delta"]
    finally:
        window.root.destroy()


# --- comparison filters and discarded models ------------------------------------------------

def test_column_values_and_filters_work_like_excel():
    from simulator import column_values, row_passes
    rows = [{"sort": {"model": name, "return": value, "trades": trades}, "display": {
        "model": name, "return": f"{value:+.2%}", "trades": str(trades)}}
        for name, value, trades in (("b", 2.5, 14), ("a", -0.1, 3), ("c", 0.4, 14))]

    assert column_values(rows, "model") == ["a", "b", "c"]  # text A to Z
    assert column_values(rows, "trades") == ["3", "14"]  # distinct, numbers smallest first
    assert column_values(rows, "return") == ["-10.00%", "+40.00%", "+250.00%"]

    kept = lambda filters: [row["sort"]["model"] for row in rows if row_passes(row, filters)]  # noqa: E731
    assert kept({"trades": {"values": {"14"}, "test": None}}) == ["b", "c"]
    assert kept({"return": {"values": None, "test": ("greater than", 100, None)}}) == ["b"]  # percent as shown
    assert kept({"return": {"values": None, "test": ("between", 0, 50)}}) == ["c"]
    assert kept({"trades": {"values": {"14"}, "test": None},
                 "return": {"values": None, "test": ("at most", 40, None)}}) == ["c"]
    assert kept({}) == ["b", "a", "c"]


def _shown_models(window):
    return [window.comparison_rows[item]["result"]["model_name"] for item in window.comparison_table.get_children()]


def test_a_heading_drop_down_filters_by_checked_values_and_marks_the_column(window):
    popup = window.filter_popup = __import__("simulator").ColumnFilterPopup(window, "model")
    assert popup.values == ["alpha", "beta", "gamma"] and popup.checked == {"alpha", "beta", "gamma"}

    popup.toggle("beta")
    popup.apply()

    assert sorted(_shown_models(window)) == ["alpha", "gamma"]
    assert window.comparison_count_var.get() == "Showing 2 of 3 models"
    assert window.comparison_table.heading("model", "text") == "[Model] \u25bc"
    assert window.comparison_table.heading("trades", "text") == "Trades \u25be"
    window._sort_comparison("trades")  # sorting keeps hidden rows hidden
    assert len(window.comparison_table.get_children()) == 2

    window._clear_comparison_filters()
    assert len(window.comparison_table.get_children()) == 3
    assert window.comparison_table.heading("model", "text") == "Model \u25be"


def test_searching_the_drop_down_keeps_only_the_checked_search_results(window):
    from simulator import ColumnFilterPopup
    popup = ColumnFilterPopup(window, "model")
    popup.search_var.set("gam")
    assert popup.visible() == ["gamma"]

    popup.apply()

    assert _shown_models(window) == ["gamma"]


def test_select_all_and_an_empty_choice(window):
    from simulator import ColumnFilterPopup
    popup = ColumnFilterPopup(window, "model")
    popup.select_all(False)
    popup.apply()
    assert popup.note_var.get() == "Check at least one value" and len(_shown_models(window)) == 3
    popup.select_all(True)
    popup.apply()
    assert "model" not in window.column_filters  # everything checked is no filter


def test_a_number_filter_and_values_offered_after_other_filters(window):
    from simulator import ColumnFilterPopup
    trades = sorted(row["sort"]["trades"] for row in window.comparison_rows.values())
    popup = ColumnFilterPopup(window, "trades")
    popup.test_var.set("at least")
    popup.first_var.set(str(trades[-1]))
    popup.apply()
    assert all(window.comparison_rows[item]["sort"]["trades"] >= trades[-1]
               for item in window.comparison_table.get_children())

    other = ColumnFilterPopup(window, "model")  # offers only the models the trades filter left
    assert len(other.values) == len(window.comparison_table.get_children())
    other.close()

    broken = ColumnFilterPopup(window, "return")
    broken.test_var.set("between")
    broken.first_var.set("5")
    broken.apply()
    assert "needs a number" in broken.note_var.get()
    broken.close()


def test_sorting_from_the_drop_down(window):
    from simulator import ColumnFilterPopup
    ColumnFilterPopup(window, "model").sort(descending=False)
    assert _shown_models(window) == ["alpha", "beta", "gamma"]
    ColumnFilterPopup(window, "model").sort(descending=True)
    assert _shown_models(window) == ["gamma", "beta", "alpha"]


def test_discarded_models_are_listed_only_when_asked(monkeypatch, tmp_path):
    window, config = _library_window(monkeypatch, tmp_path)
    try:
        (config / "BTC-USD" / "discard").mkdir()
        (config / "BTC-USD" / "discard" / "old.json").write_text(
            json.dumps({**_profile(), "product_id": "BTC-USD"}), encoding="utf-8")
        window._refresh_models()
        assert "old" not in _names(window)

        window.include_discarded_var.set(True)
        window._refresh_models()

        rows = [window.model_list.get(i) for i in range(window.model_list.size())]
        assert any(row.split()[0] == "old" and "discarded" in row for row in rows)
    finally:
        window.root.destroy()



# --- the two-list model picker -----------------------------------------------------------------

def test_models_move_between_the_available_and_selected_lists(monkeypatch, tmp_path):
    window, _ = _library_window(monkeypatch, tmp_path)
    try:
        window._remove_all_models()
        assert _names(window, which="selected") == [] and sorted(_names(window)) == ["alpha", "beta", "delta", "gamma"]
        assert window.available_count_var.get() == "Available: 4" and window.selected_count_var.get() == "Selected: 0"

        window.model_list.select_set(_names(window).index("gamma"))
        window._add_models()
        window.model_list.select_set(_names(window).index("alpha"))
        window._add_models()

        assert _names(window, which="selected") == ["gamma", "alpha"]  # in the order they were added
        assert "gamma" not in _names(window)  # moved, not highlighted
        assert [Path(p).stem for p in window.selected_model_paths()] == ["gamma", "alpha"]

        window.selected_list.select_set(1)
        window._move_selected(-1)
        assert _names(window, which="selected") == ["alpha", "gamma"]
        assert window.selected_list.curselection() == (0,)  # the moved model stays highlighted

        window.selected_list.select_set(0)
        window._remove_models()
        assert _names(window, which="selected") == ["gamma"] and "alpha" in _names(window)
    finally:
        window.root.destroy()


def test_add_all_moves_only_what_the_filter_and_search_show(monkeypatch, tmp_path):
    window, _ = _library_window(monkeypatch, tmp_path)
    try:
        window._remove_all_models()
        window.currency_var.set("BTC-USD")
        window.model_search_var.set("a")  # alpha, beta, gamma all contain "a"; delta is ETH
        window.model_search_var.set("ta")  # beta only
        assert _names(window) == ["beta"] and window.available_count_var.get() == "Available: 1 of 4"

        window._add_all_models()

        assert _names(window, which="selected") == ["beta"]
        window.model_search_var.set("")
        window.currency_var.set("All currencies")
        window._fill_model_list()
        assert sorted(_names(window)) == ["alpha", "delta", "gamma"]
    finally:
        window.root.destroy()


def test_selecting_models_another_way_fills_the_selected_list(monkeypatch, tmp_path):
    window, config = _library_window(monkeypatch, tmp_path)
    try:
        window.select_models([config / "ETH-USD" / "delta.json", config / "BTC-USD" / "beta.json"])

        assert _names(window, which="selected") == ["delta", "beta"]
        assert sorted(_names(window)) == ["alpha", "gamma"]
    finally:
        window.root.destroy()


def test_model_matches_combines_the_currency_and_type_filters():
    from simulator import ALL_CURRENCIES, ALL_MODELS, REGIME_MODELS, STANDARD_MODELS, model_matches

    assert model_matches("BTC-USD", True, ALL_CURRENCIES, ALL_MODELS)
    assert model_matches("BTC-USD", True, "BTC-USD", REGIME_MODELS)
    assert not model_matches("BTC-USD", False, "BTC-USD", REGIME_MODELS)
    assert model_matches("BTC-USD", False, ALL_CURRENCIES, STANDARD_MODELS)
    assert not model_matches("BTC-USD", True, ALL_CURRENCIES, STANDARD_MODELS)
    assert not model_matches("ETH-USD", True, "BTC-USD", REGIME_MODELS)


def test_the_model_list_filters_regime_models_by_type_and_currency(monkeypatch, tmp_path):
    import simulator
    config = tmp_path / "library"
    regimes = {"list": [{"name": "dip", "when": "close < 90"}]}
    for name, product, folder, regime in (("alpha", "BTC-USD", "", False), ("switcher", "BTC-USD", "regimes", True),
                                          ("delta", "ETH-USD", "", False), ("eth_switcher", "ETH-USD", "regimes", True)):
        target = config / product / folder
        target.mkdir(parents=True, exist_ok=True)
        profile = {**_profile(), "product_id": product, **({"regimes": regimes} if regime else {})}
        (target / f"{name}.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(simulator, "CONFIG_DIRECTORY", config)
    monkeypatch.setattr(simulator, "load_environment_config", lambda: {})
    root = tk.Tk()
    root.geometry("+-3000+-3000")
    window = simulator.SimulationWindow(root)
    try:
        assert list(window.model_type_picker["values"]) == simulator.MODEL_TYPES
        window.selected_paths = set()

        def shown(currency, model_type):
            window.currency_var.set(currency)
            window.model_type_var.set(model_type)
            window._fill_model_list()
            return sorted(_names(window))

        assert shown("All currencies", simulator.REGIME_MODELS) == ["eth_switcher", "switcher"]
        assert shown("All currencies", simulator.STANDARD_MODELS) == ["alpha", "delta"]
        assert shown("BTC-USD", simulator.REGIME_MODELS) == ["switcher"]
        assert shown("ETH-USD", simulator.ALL_MODELS) == ["delta", "eth_switcher"]
        # Regime models are tagged in the list.
        assert "(ETH-USD, regimes)" in window.model_list.get(1)
        # Add all moves only what the filters show.
        window.currency_var.set("All currencies")
        window.model_type_var.set(simulator.REGIME_MODELS)
        window._fill_model_list()
        window._add_all_models()
        assert sorted(_names(window, which="selected")) == ["eth_switcher", "switcher"]
    finally:
        window.root.destroy()

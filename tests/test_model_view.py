"""Choosing which models the charts show: the Choose models dialog and the comparison table."""

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
def window(monkeypatch):
    import simulator

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

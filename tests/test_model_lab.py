import json
import sys
import threading
import time

import pytest

import model_lab
from model_lab import Job, QueueRunner, discover, load_queue, save_queue, search_command
from model_search import EXAMPLE_SETTINGS

EXAMPLE = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))


def _settings_dir(tmp_path):
    folder = tmp_path / "search_config"
    folder.mkdir()
    (folder / "good.json").write_text(json.dumps(EXAMPLE), encoding="utf-8")
    broken = json.loads(json.dumps(EXAMPLE))
    del broken["search"]["population"]
    (folder / "broken.json").write_text(json.dumps(broken), encoding="utf-8")
    no_template = json.loads(json.dumps(EXAMPLE))
    no_template["market"]["template_profile"] = "config/nowhere.json"
    (folder / "no_template.json").write_text(json.dumps(no_template), encoding="utf-8")
    (folder / "goals_ok.json").write_text(json.dumps({"risk": {"max_drawdown_pct": 50}}), encoding="utf-8")
    (folder / "goals_bad.json").write_text(json.dumps({"risk": {"max_dd": 50}}), encoding="utf-8")
    (folder / "notes.json").write_text("{not json", encoding="utf-8")
    return folder


def test_discover_sorts_searches_from_goals_and_explains_problems(tmp_path):
    searches, goals, problems = discover(_settings_dir(tmp_path))

    by_name = {entry.path.name: entry for entry in searches}
    assert by_name["good.json"].problem == "" and by_name["good.json"].product == "BTC-USD"
    assert "search.population" in by_name["broken.json"].problem
    assert "not found" in by_name["no_template.json"].problem
    assert [path.name for path in goals] == ["goals_ok.json"]
    assert sorted(path.name for path, _ in problems) == ["goals_bad.json", "notes.json"]


def test_the_queue_survives_a_restart_and_a_running_job_is_queued_again(tmp_path):
    path = tmp_path / "queue.json"
    jobs = [Job("search_config/a.json", status="done", key=1), Job("search_config/b.json", status="running", key=2)]

    save_queue(jobs, path)
    loaded = load_queue(path)

    assert [(job.settings, job.status) for job in loaded] == [("search_config/a.json", "done"),
                                                             ("search_config/b.json", "queued")]
    assert loaded[1].note == "interrupted; queued again"
    assert load_queue(tmp_path / "missing.json") == []


def test_a_job_becomes_a_model_search_command():
    job = Job("search_config/doge.json", goals="search_config/goals_doge.json", rank=False)

    command = search_command(job, workers=5)

    assert command[1].endswith("model_search.py")
    assert command[2:] == ["--settings", "search_config/doge.json", "--generators", "5", "--evaluators", "5",
                           "--goals", "search_config/goals_doge.json", "--no-rank"]
    ranked = search_command(Job("search_config/doge.json", rank=True), workers=2, offline=True)
    assert "--no-rank" not in ranked and "--goals" not in ranked and ranked[-1] == "--offline"


def _fake(script):
    return lambda job: [sys.executable, "-c", script.replace("NAME", job.name)]


def _run(jobs, concurrency, command, timeout=20):
    events = []
    runner = QueueRunner(jobs, concurrency, command, events.append)
    runner.start()
    deadline = time.monotonic() + timeout
    while ("idle",) not in events and time.monotonic() < deadline:
        time.sleep(0.05)
    return runner, events


def test_the_runner_runs_the_queue_in_order_with_its_output_and_outcome():
    jobs = [Job(f"search_config/job{n}.json", key=n) for n in range(1, 4)]
    jobs[1].status = "done"  # finished earlier: not run again
    command = lambda job: [sys.executable, "-c", "import sys; print('hello from', sys.argv[1]); "
                                               "sys.exit(3 if sys.argv[1] == 'job3' else 0)", job.name]

    _, events = _run(jobs, 1, command)

    assert [job.status for job in jobs] == ["done", "done", "failed"]
    assert ("line", jobs[0], "hello from job1") in events and ("line", jobs[2], "hello from job3") in events
    assert jobs[2].note == "hello from job3" and jobs[0].seconds > 0
    assert [e[1].name for e in events if e[0] == "start"] == ["job1", "job3"]
    assert events[-1] == ("idle",)


def test_the_runner_runs_up_to_the_chosen_number_at_once():
    jobs = [Job(f"search_config/job{n}.json", key=n) for n in range(1, 5)]
    running, most = set(), [0]
    lock = threading.Lock()

    def report(event):
        with lock:
            if event[0] == "start":
                running.add(event[1].key)
                most[0] = max(most[0], len(running))
            elif event[0] == "end":
                running.discard(event[1].key)

    runner = QueueRunner(jobs, 2, _fake("import time; time.sleep(0.6)"), report)
    runner.start()
    runner._thread.join(timeout=20)

    assert most[0] == 2 and all(job.status == "done" for job in jobs)


def test_stop_ends_the_running_searches():
    jobs = [Job("search_config/slow.json", key=1), Job("search_config/next.json", key=2)]
    runner = QueueRunner(jobs, 1, _fake("import time; print('working', flush=True); time.sleep(60)"),
                         lambda event: None)
    runner.start()
    deadline = time.monotonic() + 10
    while not runner.running and time.monotonic() < deadline:
        time.sleep(0.05)

    runner.stop()
    runner._thread.join(timeout=20)

    assert not runner.is_alive()
    assert jobs[0].status == "stopped" and jobs[1].status == "queued"


# --- the window ---------------------------------------------------------------------------

tk = pytest.importorskip("tkinter")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.geometry("+-3000+-3000")
    monkeypatch.setattr(model_lab, "model_names_for", lambda product: set())
    window = model_lab.ModelLabWindow(root, settings_dir=_settings_dir(tmp_path), queue_file=tmp_path / "queue.json",
                                      command=_fake("print('searching NAME')"))
    yield window
    root.destroy()


def _wait_idle(window, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.root.update()
        if window.runner is not None and not window.runner.is_alive() and window.events.empty():
            window.root.update()
            return
        time.sleep(0.05)


def test_the_window_lists_runnable_settings_and_explains_the_rest(lab):
    assert list(lab.settings_picker["values"]) == ["search_config/good.json"] or all(
        value.endswith("good.json") for value in lab.settings_picker["values"])
    assert "goals_ok.json" in " ".join(lab.goals_picker["values"])
    problems = lab.problems_var.get()
    assert "broken.json" in problems and "no_template.json" in problems and "goals_bad.json" in problems


def test_queued_searches_run_and_the_queue_is_saved(lab, tmp_path, monkeypatch):
    assessed = []
    monkeypatch.setattr(model_lab, "top_models", lambda product, goals, **kwargs: assessed.append(product) or [])
    lab.rank_var.set(True)
    lab.add_job()
    lab.add_job()

    lab.start()
    _wait_idle(lab)

    assert [job.status for job in lab.jobs] == ["done", "done"]
    saved = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert [record["status"] for record in saved] == ["done", "done"] and saved[0]["rank"] is True
    assert "[good] searching good" in lab.log.get("1.0", "end")
    deadline = time.monotonic() + 10
    while len(assessed) < 2 and time.monotonic() < deadline:  # each finished search refreshes the top 20
        lab.root.update()
        time.sleep(0.05)
    assert assessed == ["BTC-USD", "BTC-USD"]


def test_the_top_table_shows_assessments(lab):
    assessment = {"model": "btc_007", "product": "BTC-USD", "passes": True, "goals_met": 3, "goals_set": 3,
                  "score": 0.41, "metrics": {"annual_growth_pct": 55.0, "ending_value": 250_000.0,
                                             "growth_multiple": 25.0, "max_drawdown_pct": 31.0, "trades": 14.0},
                  "recent": {"return_pct": 12.5}, "missed": []}

    lab._show_top("BTC-USD", "search_config/goals_ok.json", False, [assessment])

    row = lab.top_table.item("0", "values")
    assert row[1] == "btc_007" and row[3] == "yes" and row[4] == "3/3" and row[7] == "25.00x" and row[10] == "+12.5%"
    assert "1 pass" in lab.top_note_var.get()


def test_queue_order_can_be_changed_and_finished_searches_cleared(lab):
    for _ in range(3):
        lab.add_job()
    first, second, third = (job.key for job in lab.jobs)
    lab.queue_table.selection_set(str(third))
    lab.move(-1)
    assert [job.key for job in lab.jobs] == [first, third, second]
    lab.jobs[0].status = "done"
    lab.clear_finished()
    assert [job.key for job in lab.jobs] == [third, second]

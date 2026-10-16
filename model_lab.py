"""Model Lab: queue model searches, run them on this computer, and see the top 20 models.

    python model_lab.py

Search settings and goals files live in search_config/ (see search_config/README.md); write
them by hand or have an assistant draft them. Model Lab does the rest locally:

  1. Pick a settings file, optionally a goals file, and add it to the queue. The queue is
     kept in data/model_lab/queue.json between sessions.
  2. Start the queue. Each search runs as `python model_search.py` in its own process,
     several at once if you choose: "Searches at once" shares the CPUs between them, each
     search getting its share of generator and evaluator workers. Stop ends the running
     searches and their workers.
  3. When a search finishes, the Top 20 tab assesses its currency's models against the
     goals (model_assessor.py) and lists the 20 best distinct models: only the models this
     queue made, or every model of the currency.

Searches use live fees and download new candles, like the simulator, so the .env API key
is needed; "Offline" uses cached candles and each profile's fallback fee instead, which
understates fees, so treat offline results as previews.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
SETTINGS_DIR = ROOT / "search_config"
LAB_DIR = ROOT / "data" / "model_lab"
QUEUE_FILE = LAB_DIR / "queue.json"
TOP_COUNT = 20
SETTINGS_OWN_GOALS = "(the settings' own goals)"
GOAL_SECTIONS = {"growth", "ending_value", "risk", "recent", "ranking", "require"}


# --- settings and goals files ---------------------------------------------------------

@dataclass
class SearchFile:
    path: Path
    product: str = ""
    prefix: str = ""
    goals: str = ""  # the settings' own "goals" entry
    problem: str = ""  # why it can't run, or ""


def discover(folder=SETTINGS_DIR):
    """(searches, goals files, other problems) among the JSON files in `folder`.

    A file with a "market" section is a search settings file, checked as model_search.py
    would; one with only goal sections is a goals file, checked as model_assessor.py would.
    """
    from model_assessor import GoalsError, check_goals
    from model_search import SettingsError, load_settings, profile_path, with_optional_settings
    searches, goals, problems = [], [], []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append((path, f"not valid JSON: {exc}"))
            continue
        if not isinstance(data, dict):
            continue
        if "market" in data:
            entry = SearchFile(path)
            try:
                settings = with_optional_settings(load_settings(path))
                entry.product, entry.prefix = settings["market"]["product"], settings["market"]["prefix"]
                entry.goals = settings.get("goals") or ""
                if not profile_path(settings["market"]["template_profile"]).exists():
                    entry.problem = f"template_profile {settings['market']['template_profile']} not found"
            except (SettingsError, KeyError, TypeError, ValueError) as exc:
                entry.problem = str(exc)
            searches.append(entry)
        elif GOAL_SECTIONS & set(data):
            try:
                check_goals(data, path.name)
                goals.append(path)
            except GoalsError as exc:
                problems.append((path, str(exc)))
    return searches, goals, problems


def relative(path):
    """`path` relative to the repository, as the tools' command lines take it."""
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


# --- the queue ------------------------------------------------------------------------

@dataclass
class Job:
    settings: str  # path from the repository root
    goals: str = ""  # goals file from the repository root; "" uses the settings' own
    rank: bool = False  # rank the currency's models and discard the bottom when the search ends
    product: str = ""
    prefix: str = ""
    status: str = "queued"  # queued, running, done, failed, stopped
    note: str = ""
    seconds: float = 0.0
    key: int = field(default=0)

    @property
    def name(self):
        return Path(self.settings).stem


def load_queue(path=QUEUE_FILE):
    """The saved queue; a job that was running when Model Lab closed is queued again."""
    try:
        records = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = []
    for number, record in enumerate(records, start=1):
        try:
            job = Job(**{key: value for key, value in record.items() if key in Job.__dataclass_fields__})
        except TypeError:
            continue
        job.key = number
        if job.status == "running":
            job.status, job.note = "queued", "interrupted; queued again"
        jobs.append(job)
    return jobs


def save_queue(jobs, path=QUEUE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps([asdict(job) for job in jobs], indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def search_command(job, workers, offline=False):
    """The command line that runs one queued search with `workers` generators and evaluators."""
    command = [sys.executable, str(ROOT / "model_search.py"), "--settings", job.settings,
               "--generators", str(workers), "--evaluators", str(workers)]
    if job.goals:
        command += ["--goals", job.goals]
    if not job.rank:
        command.append("--no-rank")
    if offline:
        command.append("--offline")
    return command


def kill_tree(process):
    """End a search and the worker processes it started."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()


class QueueRunner:
    """Runs queued jobs, up to `concurrency` at a time, each as its own process.

    Reports through `report(event)`, from its own threads: ("start", job), ("line", job,
    text), ("end", job) and finally ("idle",). `command(job)` builds each command line.
    """

    def __init__(self, jobs, concurrency, command, report):
        self.jobs, self.concurrency, self.command, self.report = jobs, max(1, concurrency), command, report
        self.running = {}  # job key -> process
        self.stopping = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="model-lab-queue", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """Stop starting jobs and end the running ones; they are marked stopped."""
        self.stopping = True
        with self._lock:
            processes = list(self.running.values())
        for process in processes:
            kill_tree(process)

    def is_alive(self):
        return self._thread.is_alive()

    def _run(self):
        threads = []
        while not self.stopping:
            # Counted from the jobs' status, set here before their process starts, so the limit holds.
            busy = sum(job.status == "running" for job in self.jobs)
            waiting = [job for job in self.jobs if job.status == "queued"]
            if not waiting and not busy:
                break
            if waiting and busy < self.concurrency:
                job = waiting[0]
                job.status, job.note, job.seconds = "running", "", 0.0
                thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
                threads.append(thread)
                thread.start()
                continue
            time.sleep(0.2)
        for thread in threads:
            thread.join()
        self.report(("idle",))

    def _run_job(self, job):
        started = time.monotonic()
        with self._lock:
            if self.stopping:  # stopped before its process started: it stays queued
                job.status = "queued"
                return
        self.report(("start", job))
        try:
            options = {"start_new_session": True} if os.name != "nt" else {}
            process = subprocess.Popen(self.command(job), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1, **options)
        except OSError as exc:
            job.status, job.note = "failed", f"could not start: {exc}"
            self.report(("end", job))
            return
        with self._lock:
            self.running[job.key] = process
            stopped_meanwhile = self.stopping
        if stopped_meanwhile:
            kill_tree(process)
        last = ""
        for line in process.stdout:
            line = line.rstrip()
            if line:
                last = line
                self.report(("line", job, line))
        code = process.wait()
        with self._lock:
            self.running.pop(job.key, None)
        job.seconds = time.monotonic() - started
        if self.stopping:
            job.status, job.note = "stopped", "stopped by you"
        elif code == 0:
            job.status, job.note = "done", ""
        else:
            job.status, job.note = "failed", last[:200] or f"exit code {code}"
        self.report(("end", job))


# --- the top models -------------------------------------------------------------------

def top_models(product, goals_path, names=None, offline=False, workers=None, count=TOP_COUNT, progress=None):
    """The best `count` distinct models of `product` against the goals file, as model_assessor ranks them.

    `names` limits them to those models (say, the ones a queue made). The ranking reads the
    performance log; a goals file with a "recent" period also backtests every model from its
    start (with live fees, or `offline` with fallback fees).
    """
    from model_assessor import (assess_models, check_goals, config_models, load_goals, log_rows, mark_duplicates,
                                rank, recent_results, recent_start)
    goals = load_goals(ROOT / goals_path) if goals_path else check_goals({})
    models = config_models(product)
    if names is not None:
        models = [model for model in models if model[0] in names]
    if not models:
        return []
    recent = {}
    if recent_start(goals) is not None:
        from parallel_backtest import market_inputs
        client, inputs = market_inputs([profile for _, _, profile in models], offline)
        recent = recent_results(models, goals, inputs, client, workers, progress)
    ranked = mark_duplicates(rank(assess_models(models, goals, log_rows(), recent)))
    distinct = [assessment for assessment in ranked if not assessment.get("duplicate_of")]
    return distinct[:count]


def model_names_for(product):
    from model_assessor import config_models
    return {name for name, _, _ in config_models(product)}


# --- the window -----------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - headless environments
    tk = None


TOP_COLUMNS = [
    ("rank", "#", 36, "e"), ("model", "Model", 170, "w"), ("product", "Currency", 80, "w"),
    ("passes", "Pass", 44, "center"), ("goals", "Goals", 54, "center"), ("annual", "Annual", 70, "e"),
    ("ending", "Ending value", 100, "e"), ("growth", "Growth", 64, "e"), ("drawdown", "Max DD", 64, "e"),
    ("trades", "Trades", 54, "e"), ("recent", "Recent", 70, "e"), ("score", "Score", 64, "e"),
    ("missed", "Missed targets", 260, "w"),
]
QUEUE_COLUMNS = [
    ("number", "#", 30, "e"), ("settings", "Settings", 170, "w"), ("product", "Currency", 80, "w"),
    ("goals", "Goals", 170, "w"), ("rank", "Rank + discard", 90, "center"), ("status", "Status", 70, "w"),
    ("time", "Time", 64, "e"), ("note", "Note", 260, "w"),
]


class ModelLabWindow:
    """Queue searches, run them, watch their output and browse the top models."""

    def __init__(self, root, settings_dir=SETTINGS_DIR, queue_file=QUEUE_FILE, command=None):
        from parallel_backtest import max_workers
        self.root, self.settings_dir, self.queue_file = root, Path(settings_dir), Path(queue_file)
        self.command_for = command  # tests replace the search command
        self.max_workers = max_workers()
        self.events = queue.Queue()
        self.runner = None
        self.jobs = load_queue(self.queue_file)
        self.next_key = max((job.key for job in self.jobs), default=0) + 1
        self.searches, self.goals_files, self.problems = [], [], []
        self.names_before = {}  # product -> model names when the queue started
        self.made = {}  # product -> names the queue's searches made
        self.top = []
        root.title("Model Lab")
        root.geometry("1250x900")
        root.minsize(1000, 700)
        self._build()
        self.reload()
        self._show_queue()
        self.root.after(150, self._poll)

    # --- layout ---------------------------------------------------------------------

    def _build(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        pick = ttk.LabelFrame(main, text="Add a search to the queue", padding=8)
        pick.pack(fill="x")
        pick.columnconfigure(1, weight=1)
        ttk.Label(pick, text="Search settings:").grid(row=0, column=0, sticky="w")
        self.settings_var = tk.StringVar()
        self.settings_picker = ttk.Combobox(pick, textvariable=self.settings_var, state="readonly")
        self.settings_picker.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.settings_picker.bind("<<ComboboxSelected>>", lambda _event: self._describe_choice())
        ttk.Label(pick, text="Goals:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.goals_var = tk.StringVar(value=SETTINGS_OWN_GOALS)
        self.goals_picker = ttk.Combobox(pick, textvariable=self.goals_var, state="readonly")
        self.goals_picker.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        self.rank_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pick, text="When it ends, rank the currency's models and move the bottom 80% to its "
                                   "discard folder", variable=self.rank_var).grid(row=2, column=1, sticky="w",
                                                                                pady=(6, 0))
        buttons = ttk.Frame(pick)
        buttons.grid(row=0, column=2, rowspan=3, sticky="ne", padx=(10, 0))
        ttk.Button(buttons, text="Add to queue", command=self.add_job).pack(fill="x")
        ttk.Button(buttons, text="Reload files", command=self.reload).pack(fill="x", pady=(4, 0))
        self.choice_var = tk.StringVar()
        ttk.Label(pick, textvariable=self.choice_var, foreground="#555555").grid(row=3, column=1, sticky="w",
                                                                                pady=(6, 0))
        self.problems_var = tk.StringVar()
        ttk.Label(pick, textvariable=self.problems_var, foreground="#b3261e", wraplength=1000, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        queued = ttk.LabelFrame(main, text="Queue", padding=8)
        queued.pack(fill="x", pady=(10, 0))
        table = ttk.Frame(queued)
        table.pack(fill="x")
        self.queue_table = ttk.Treeview(table, columns=[c for c, *_ in QUEUE_COLUMNS], show="headings", height=6,
                                        selectmode="extended")
        for key, heading, width, anchor in QUEUE_COLUMNS:
            self.queue_table.heading(key, text=heading)
            self.queue_table.column(key, width=width, anchor=anchor, stretch=key == "note")
        self.queue_table.pack(side="left", fill="x", expand=True)
        side = ttk.Frame(table)
        side.pack(side="left", padx=(8, 0), anchor="n")
        ttk.Button(side, text="Move up", command=lambda: self.move(-1)).pack(fill="x")
        ttk.Button(side, text="Move down", command=lambda: self.move(1)).pack(fill="x", pady=(4, 0))
        ttk.Button(side, text="Remove", command=self.remove).pack(fill="x", pady=(4, 0))
        ttk.Button(side, text="Run again", command=self.requeue).pack(fill="x", pady=(4, 0))
        ttk.Button(side, text="Clear finished", command=self.clear_finished).pack(fill="x", pady=(4, 0))

        controls = ttk.Frame(queued)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Label(controls, text="Searches at once:").pack(side="left")
        self.concurrency_var = tk.StringVar(value="1")
        ttk.Spinbox(controls, from_=1, to=max(1, self.max_workers), textvariable=self.concurrency_var, width=4,
                    command=self._describe_workers).pack(side="left", padx=(6, 0))
        self.workers_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.workers_var, foreground="#555555").pack(side="left", padx=(8, 0))
        self.offline_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Offline (cached candles, fallback fees: a preview only)",
                        variable=self.offline_var).pack(side="left", padx=(16, 0))
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.start_button = ttk.Button(controls, text="Start queue", command=self.start)
        self.start_button.pack(side="right", padx=(0, 6))
        self._describe_workers()

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(main, textvariable=self.status_var, foreground="#1a5d1a").pack(side="bottom", anchor="w",
                                                                                pady=(6, 0))

        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
        top_tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(top_tab, text=f"Top {TOP_COUNT}")
        log_tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(log_tab, text="Log")

        top_controls = ttk.Frame(top_tab)
        top_controls.pack(fill="x")
        ttk.Label(top_controls, text="Currency:").pack(side="left")
        self.top_product_var = tk.StringVar()
        self.top_product_picker = ttk.Combobox(top_controls, textvariable=self.top_product_var, state="readonly",
                                               width=12)
        self.top_product_picker.pack(side="left", padx=(6, 12))
        ttk.Label(top_controls, text="Goals:").pack(side="left")
        self.top_goals_var = tk.StringVar()
        self.top_goals_picker = ttk.Combobox(top_controls, textvariable=self.top_goals_var, state="readonly",
                                             width=36)
        self.top_goals_picker.pack(side="left", padx=(6, 12))
        self.scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(top_controls, text="All models of the currency", value="all",
                        variable=self.scope_var).pack(side="left")
        ttk.Radiobutton(top_controls, text="Only this queue's new models", value="new",
                        variable=self.scope_var).pack(side="left", padx=(6, 12))
        ttk.Button(top_controls, text="Assess", command=self.refresh_top).pack(side="left")
        ttk.Button(top_controls, text="Open selected in simulator", command=self.open_in_simulator).pack(side="right")
        self.top_note_var = tk.StringVar(value="Run a search, or pick a currency and click Assess")
        ttk.Label(top_tab, textvariable=self.top_note_var, foreground="#555555").pack(anchor="w", pady=(6, 4))
        frame = ttk.Frame(top_tab)
        frame.pack(fill="both", expand=True)
        self.top_table = ttk.Treeview(frame, columns=[c for c, *_ in TOP_COLUMNS], show="headings",
                                      selectmode="extended")
        for key, heading, width, anchor in TOP_COLUMNS:
            self.top_table.heading(key, text=heading)
            self.top_table.column(key, width=width, anchor=anchor, stretch=key == "missed")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.top_table.yview)
        self.top_table.configure(yscrollcommand=scroll.set)
        self.top_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.top_table.bind("<Double-1>", lambda _event: self.open_in_simulator())

        self.log = tk.Text(log_tab, wrap="none", height=12, bg="#f5f5f5")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="left", fill="y")

    # --- files ----------------------------------------------------------------------

    def reload(self):
        """Read search_config/ again, for files written or edited since."""
        self.searches, self.goals_files, self.problems = discover(self.settings_dir)
        runnable = [entry for entry in self.searches if not entry.problem]
        self.settings_picker["values"] = [relative(entry.path) for entry in runnable]
        if runnable and self.settings_var.get() not in self.settings_picker["values"]:
            self.settings_var.set(relative(runnable[0].path))
        goals = [relative(path) for path in self.goals_files]
        self.goals_picker["values"] = [SETTINGS_OWN_GOALS, *goals]
        self.top_goals_picker["values"] = ["(no goals: rank by score)", *goals]
        if not self.top_goals_var.get():
            self.top_goals_var.set(goals[0] if goals else "(no goals: rank by score)")
        products = sorted({entry.product for entry in runnable if entry.product})
        self.top_product_picker["values"] = products
        if products and self.top_product_var.get() not in products:
            self.top_product_var.set(products[0])
        broken = [(entry.path, entry.problem) for entry in self.searches if entry.problem] + self.problems
        self.problems_var.set("" if not broken else "Files that can't be used: " + "; ".join(
            f"{Path(path).name}: {problem[:160]}" for path, problem in broken))
        self._describe_choice()

    def _chosen(self):
        return next((entry for entry in self.searches if relative(entry.path) == self.settings_var.get()), None)

    def _describe_choice(self):
        entry = self._chosen()
        if entry is None:
            self.choice_var.set("No runnable settings files in search_config/ yet")
            return
        self.choice_var.set(f"{entry.product}, models named {entry.prefix}_###; own goals: "
                            f"{entry.goals or 'none (search_config/goals.json if present)'}")

    def _describe_workers(self):
        searches = self._concurrency()
        each = max(1, self.max_workers // searches)
        self.workers_var.set(f"each search gets {each} generator and {each} evaluator workers "
                             f"({self.max_workers} for searches on this computer)")

    def _concurrency(self):
        try:
            return max(1, min(int(self.concurrency_var.get()), self.max_workers))
        except ValueError:
            return 1

    # --- the queue ------------------------------------------------------------------

    def add_job(self):
        entry = self._chosen()
        if entry is None:
            return
        goals = "" if self.goals_var.get() == SETTINGS_OWN_GOALS else self.goals_var.get()
        job = Job(settings=relative(entry.path), goals=goals, rank=self.rank_var.get(), product=entry.product,
                  prefix=entry.prefix, key=self.next_key)
        self.next_key += 1
        self.jobs.append(job)
        self._changed()

    def _selected_jobs(self):
        keys = {int(item) for item in self.queue_table.selection()}
        return [job for job in self.jobs if job.key in keys]

    def move(self, step):
        for job in sorted(self._selected_jobs(), key=self.jobs.index, reverse=step > 0):
            index = self.jobs.index(job)
            target = index + step
            if 0 <= target < len(self.jobs):
                self.jobs[index], self.jobs[target] = self.jobs[target], self.jobs[index]
        selection = self.queue_table.selection()
        self._changed()
        self.queue_table.selection_set(selection)

    def remove(self):
        running = [job for job in self._selected_jobs() if job.status == "running"]
        if running:
            self.status_var.set("Stop the queue before removing a running search")
        chosen = {job.key for job in self._selected_jobs() if job.status != "running"}
        self.jobs = [job for job in self.jobs if job.key not in chosen]
        self._changed()

    def requeue(self):
        for job in self._selected_jobs():
            if job.status != "running":
                job.status, job.note, job.seconds = "queued", "", 0.0
        self._changed()

    def clear_finished(self):
        self.jobs = [job for job in self.jobs if job.status in ("queued", "running")]
        self._changed()

    def _changed(self):
        try:
            save_queue(self.jobs, self.queue_file)
        except OSError as exc:
            self.status_var.set(f"Queue not saved: {exc}")
        self._show_queue()

    def _show_queue(self):
        selection = self.queue_table.selection()
        self.queue_table.delete(*self.queue_table.get_children())
        for number, job in enumerate(self.jobs, start=1):
            minutes, seconds = divmod(int(job.seconds), 60)
            self.queue_table.insert("", "end", iid=str(job.key), values=(
                number, job.settings, job.product, job.goals or SETTINGS_OWN_GOALS, "yes" if job.rank else "no",
                job.status, f"{minutes}:{seconds:02d}" if job.seconds else "", job.note))
        existing = [item for item in selection if self.queue_table.exists(item)]
        if existing:
            self.queue_table.selection_set(existing)

    # --- running --------------------------------------------------------------------

    def start(self):
        if self.runner is not None and self.runner.is_alive():
            return
        if not any(job.status == "queued" for job in self.jobs):
            self.status_var.set("Nothing queued: add a search, or select finished ones and click Run again")
            return
        if self.offline_var.get() and messagebox is not None and not messagebox.askyesno(
                "Offline", "Offline searches use each profile's fallback fee instead of your live fee tier, which "
                           "can understate fees. Run offline anyway?"):
            return
        searches = self._concurrency()
        workers = max(1, self.max_workers // searches)
        offline = self.offline_var.get()
        command = self.command_for or (lambda job: search_command(job, workers, offline))
        for product in {job.product for job in self.jobs if job.status == "queued"}:
            self.names_before.setdefault(product, model_names_for(product))
        self.runner = QueueRunner(self.jobs, searches, command, self.events.put)
        self.runner.start()
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status_var.set(f"Running the queue, {searches} search(es) at once")

    def stop(self):
        if self.runner is not None:
            self.status_var.set("Stopping the running searches...")
            self.runner.stop()

    def _poll(self):
        """Apply the runner's and the assessor's events on the Tk thread; repeats every 150 ms."""
        lines = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "line":
                lines.append(f"[{event[1].name}] {event[2]}")
            elif kind == "start":
                lines.append(f"[{event[1].name}] --- started ---")
                self._changed()
            elif kind == "end":
                job = event[1]
                lines.append(f"[{job.name}] --- {job.status} {job.note} ---")
                self._changed()
                if job.status == "done" and job.product:
                    self.made.setdefault(job.product, set()).update(
                        model_names_for(job.product) - self.names_before.get(job.product, set()))
                    self.top_product_var.set(job.product)
                    if job.goals:
                        self.top_goals_var.set(job.goals)
                    self.refresh_top()
            elif kind == "idle":
                self.start_button.state(["!disabled"])
                self.stop_button.state(["disabled"])
                done = sum(job.status == "done" for job in self.jobs)
                self.status_var.set(f"Queue finished: {done} done, "
                                    f"{sum(job.status == 'failed' for job in self.jobs)} failed, "
                                    f"{sum(job.status == 'stopped' for job in self.jobs)} stopped")
            elif kind == "top":
                self._show_top(*event[1:])
            elif kind == "top_error":
                self.top_note_var.set(f"Assessment failed: {event[1]}")
        if lines:
            self._log(lines)
        self.root.after(150, self._poll)

    def _log(self, lines):
        self.log.configure(state="normal")
        self.log.insert("end", "\n".join(lines) + "\n")
        excess = int(self.log.index("end-1c").split(".")[0]) - 5000
        if excess > 0:
            self.log.delete("1.0", f"{excess}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # --- the top models -------------------------------------------------------------

    def refresh_top(self):
        product = self.top_product_var.get()
        if not product:
            return
        goals = self.top_goals_var.get()
        goals = "" if goals.startswith("(") else goals
        names = self.made.get(product, set()) if self.scope_var.get() == "new" else None
        offline = self.offline_var.get()
        self.top_note_var.set(f"Assessing {product} models against {goals or 'their score alone'}...")

        def work():
            try:
                top = top_models(product, goals, names=names, offline=offline)
                self.events.put(("top", product, goals, names is not None, top))
            except Exception as exc:  # shown in the window; the queue keeps running
                self.events.put(("top_error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, name="model-lab-assess", daemon=True).start()

    def _show_top(self, product, goals, new_only, top):
        self.top = top
        self.top_table.delete(*self.top_table.get_children())
        for position, assessment in enumerate(top, start=1):
            metrics, recent = assessment["metrics"], assessment.get("recent") or {}

            def show(value, form):
                return "" if value is None else format(value, form)

            self.top_table.insert("", "end", iid=str(position - 1), values=(
                position, assessment["model"], assessment["product"], "yes" if assessment["passes"] else "no",
                f"{assessment['goals_met']}/{assessment['goals_set']}" if assessment["goals_met"] >= 0 else "-",
                show(metrics.get("annual_growth_pct"), "+.1f") + ("%" if metrics else ""),
                show(metrics.get("ending_value"), ",.0f"),
                show(metrics.get("growth_multiple"), ".2f") + ("x" if metrics else ""),
                show(metrics.get("max_drawdown_pct"), ".1f") + ("%" if metrics else ""),
                show(metrics.get("trades"), ".0f"),
                show(recent.get("return_pct"), "+.1f") + ("%" if recent else ""),
                f"{assessment['score']:+.3f}", "; ".join(assessment["missed"])))
        passing = sum(assessment["passes"] for assessment in top)
        scope = "this queue's new" if new_only else "all"
        self.top_note_var.set(f"Top {len(top)} distinct {product} models ({scope}) against "
                              f"{goals or 'score alone'}: {passing} pass. Double-click to open in the simulator.")

    def open_in_simulator(self):
        from model_assessor import config_models
        chosen = [self.top[int(item)]["model"] for item in self.top_table.selection()]
        if not chosen:
            return
        paths = [str(path) for name, path, _ in config_models(self.top_product_var.get()) if name in chosen]
        subprocess.Popen([sys.executable, str(ROOT / "simulator.py"), "--models", *paths], cwd=ROOT)

    def close(self):
        if self.runner is not None and self.runner.is_alive():
            if messagebox is not None and not messagebox.askyesno(
                    "Searches running", "Searches are still running. Stop them and close?"):
                return
            self.runner.stop()
        self.root.destroy()


def main():
    if tk is None:
        raise RuntimeError("tkinter is required for Model Lab.")
    root = tk.Tk()
    window = ModelLabWindow(root)
    root.protocol("WM_DELETE_WINDOW", window.close)
    root.mainloop()


if __name__ == "__main__":
    main()

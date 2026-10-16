"""The simulator's Test options window: the data to test on, synthetic scenarios, and which models to highlight.

Opened from the simulator's "Test options..." button. Scenario sets are saved as JSON files in
scenarios/ (see synthetic.py for every scenario type and parameter).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import stress_test
import synthetic

OPTIONS_FILE = Path(__file__).parent / "data" / "simulator_options.json"
MODES = [("historic", "Historic data only"),
         ("historic+synthetic", "Historic data plus synthetic scenarios"),
         ("synthetic", "Synthetic scenarios only"),
         ("stress", "Stress test")]
DEFAULT_OPTIONS = {"mode": "historic", "scenario_set": str(synthetic.DEFAULT_SET), "top_n": 5,
                   "stress_paths": 3}
# Every scenario parameter the editor shows, in order, with a label.
FIELDS = [("drop", "Drop (0.5 = -50%)"), ("rise", "Rise (2.0 = +200%)"), ("days", "Days"),
          ("recovery_days", "Recovery days"), ("recovery", "Recovery (share of the drop)"),
          ("collapse_days", "Collapse days"), ("range", "Range (0.15 = 15% either way)"),
          ("volatility", "Volatility (1 = the market's own)"), ("paths", "Paths"), ("block_days", "Block days")]


def load_options(path=OPTIONS_FILE):
    try:
        return {**DEFAULT_OPTIONS, **json.loads(Path(path).read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_OPTIONS)


def save_options(options, path=OPTIONS_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(options, indent=2) + "\n", encoding="utf-8")


def describe(scenario):
    """A short summary of a scenario's settings, for the scenario table."""
    keys = [key for key, _ in FIELDS if key in scenario]
    where = "after history" if scenario["placement"] == "append" else f"at {scenario['at']}"
    return f"{where}; " + ", ".join(f"{key} {scenario[key]:g}" for key in keys) + f"; seed {scenario['seed']}"


def describe_mode(options):
    label = dict(MODES)[options["mode"]]
    if options["mode"] == "historic":
        return f"Data: {label.lower()}"
    if options["mode"] == "stress":
        try:
            count = len(stress_test.load_plan())
        except (OSError, ValueError):
            count = 0
        return f"Data: stress test ({count} combinations in the stress plan, alone and with history; a report per model)"
    try:
        count = len(synthetic.runs(synthetic.load_set(options["scenario_set"])))
    except (OSError, ValueError):
        count = 0
    return f"Data: {label.lower()} ({count} scenario runs in {Path(options['scenario_set']).name})"


class TestOptionsDialog:
    """Edits the simulator's test options; calls `on_save(options)` when the user clicks Save."""

    def __init__(self, root, options, on_save, markets=()):
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        self.tk, self.ttk, self.messagebox, self.simpledialog = tk, ttk, messagebox, simpledialog
        self.options, self.on_save, self.markets = dict(options), on_save, list(markets)
        self.window = tk.Toplevel(root)
        self.window.title("Test options")
        self.window.transient(root)
        self.window.geometry("1100x820")
        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)

        data = ttk.LabelFrame(frame, text="Data to test on", padding=8)
        data.pack(fill="x")
        self.mode_var = tk.StringVar(value=self.options["mode"])
        for value, label in MODES:
            ttk.Radiobutton(data, text=label, value=value, variable=self.mode_var).pack(side="left", padx=(0, 16))

        stress = ttk.LabelFrame(frame, text="Stress test", padding=8)
        stress.pack(fill="x", pady=(8, 0))
        ttk.Label(stress, text="Runs every combination of the stress test plan (its tab below), each alone and after "
                               "the full history, with up to").pack(side="left")
        self.paths_var = tk.StringVar(value=str(self.options.get("stress_paths", 3)))
        ttk.Spinbox(stress, from_=1, to=50, textvariable=self.paths_var, width=4).pack(side="left", padx=4)
        ttk.Label(stress, text="paths of resampled scenarios, and writes a report per model.").pack(side="left")

        focus = ttk.LabelFrame(frame, text="Focus", padding=8)
        focus.pack(fill="x", pady=(8, 0))
        ttk.Label(focus, text="Highlight each currency's top").pack(side="left")
        self.top_var = tk.StringVar(value=str(self.options["top_n"]))
        ttk.Spinbox(focus, from_=1, to=50, textvariable=self.top_var, width=4).pack(side="left", padx=4)
        ttk.Label(focus, text="models by historic growth (from the performance log), marked ▲ in the Models list; "
                              "\"Select top models\" selects them.", foreground="#555555").pack(side="left")

        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", command=self._save).pack(side="right", padx=(0, 6))
        self.tabs = ttk.Notebook(frame)
        self.tabs.pack(fill="both", expand=True, pady=(8, 0))
        scenarios = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(scenarios, text="Synthetic scenarios")
        plan_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(plan_tab, text="Stress test plan")
        set_row = ttk.Frame(scenarios)
        set_row.pack(fill="x")
        ttk.Label(set_row, text="Scenario set:").pack(side="left")
        self.set_var = tk.StringVar(value=Path(self.options["scenario_set"]).name)
        self.set_picker = ttk.Combobox(set_row, textvariable=self.set_var, state="readonly", width=30)
        self.set_picker.pack(side="left", padx=(6, 6))
        self.set_picker.bind("<<ComboboxSelected>>", lambda _event: self._load_set())
        ttk.Button(set_row, text="Save set", command=self._save_set).pack(side="left")
        ttk.Button(set_row, text="Save set as...", command=self._save_set_as).pack(side="left", padx=(4, 0))
        self.note_var = tk.StringVar()
        ttk.Label(set_row, textvariable=self.note_var, foreground="#b3261e").pack(side="left", padx=(12, 0))

        body = ttk.Frame(scenarios)
        body.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Drag rows to reorder. A module that continues the one above joins its run: "
                             "the run's modules play one after another as one path.",
                  foreground="#555555").pack(anchor="w")
        self.table = ttk.Treeview(left, columns=("run", "name", "type", "settings"), show="headings", height=9,
                                  selectmode="browse")
        for key, heading, width in (("run", "Run", 120), ("name", "Scenario module", 240), ("type", "Type", 70),
                                    ("settings", "Settings", 330)):
            self.table.heading(key, text=heading)
            self.table.column(key, width=width, stretch=key == "settings")
        self.table.tag_configure("chained", foreground="#1a4f8b")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda _event: self._select())
        # Drag and drop: press on a row, drag it to its new place, release.
        self.table.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.table.bind("<B1-Motion>", self._drag_motion, add="+")
        self.table.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self.dragging = None
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(4, 0))
        for text, command in (("Add new", self._add), ("Duplicate", self._duplicate), ("Remove", self._remove),
                              ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)),
                              ("Chain to above", lambda: self._set_chain(True)),
                              ("Unchain", lambda: self._set_chain(False))):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 4))

        editor = ttk.Frame(body, padding=(10, 0, 0, 0))
        editor.pack(side="left", fill="y")
        self.vars = {}
        rows = [("name", "Name"), ("type", "Type"), ("placement", "Placement (a run's first module)"),
                ("at", "At date (YYYY-MM-DD)"), ("seed", "Seed")] + FIELDS
        for row, (key, label) in enumerate(rows):
            ttk.Label(editor, text=label).grid(row=row, column=0, sticky="w", pady=1)
            variable = tk.StringVar()
            if key == "type":
                widget = ttk.Combobox(editor, textvariable=variable, values=synthetic.TYPES, state="readonly", width=24)
                widget.bind("<<ComboboxSelected>>", lambda _event: self._enable_fields())
            elif key == "placement":
                widget = ttk.Combobox(editor, textvariable=variable, values=["append", "at"], state="readonly", width=24)
                widget.bind("<<ComboboxSelected>>", lambda _event: self._enable_fields())
            else:
                widget = ttk.Entry(editor, textvariable=variable, width=27)
            widget.grid(row=row, column=1, sticky="w", pady=1)
            self.vars[key] = (variable, widget)
        self.chain_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(editor, text="Continues the scenario above (chained)", variable=self.chain_var).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(editor, text="Apply changes to the scenario", command=self._apply).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        preview = ttk.Frame(scenarios)
        preview.pack(fill="x", pady=(8, 0))
        top = ttk.Frame(preview)
        top.pack(fill="x")
        ttk.Label(top, text="Preview on:").pack(side="left")
        self.market_var = tk.StringVar(value=self.markets[0] if self.markets else "")
        ttk.Combobox(top, textvariable=self.market_var, values=self.markets, state="readonly", width=26).pack(
            side="left", padx=6)
        ttk.Button(top, text="Preview the selected module's run", command=self._preview).pack(side="left")
        figure = Figure(figsize=(9, 2.4), dpi=90)
        self.ax = figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(figure, master=preview)
        self.canvas.get_tk_widget().pack(fill="x")

        self._build_plan_tab(plan_tab)

        self.scenarios = []
        self._refresh_sets()
        self._load_set()
        self.plan = list(stress_test.load_plan())
        self._show_plan()

    # --- the stress test plan ------------------------------------------------------------------

    def _build_plan_tab(self, tab):
        tk, ttk = self.tk, self.ttk
        ttk.Label(tab, text="The combinations every stress test runs, kept in scenarios/stress_plan.json. Each "
                            "combination is its own copy of its scenario modules, so editing a scenario set doesn't "
                            "change it. Edits are kept for future runs when you click Save; Reset to default restores "
                            "the built-in baseline.", foreground="#555555", wraplength=1050,
                  justify="left").pack(anchor="w")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.plan_table = ttk.Treeview(body, columns=("number", "name", "steps", "nightmare"), show="headings",
                                       selectmode="extended")
        for key, heading, width in (("number", "#", 40), ("name", "Combination", 640), ("steps", "Modules", 70),
                                    ("nightmare", "Nightmare", 80)):
            self.plan_table.heading(key, text=heading)
            self.plan_table.column(key, width=width, stretch=key == "name",
                                   anchor="w" if key == "name" else "center")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.plan_table.yview)
        self.plan_table.configure(yscrollcommand=scroll.set)
        self.plan_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.plan_table.bind("<ButtonPress-1>", self._plan_drag_start, add="+")
        self.plan_table.bind("<B1-Motion>", self._plan_drag_motion, add="+")
        self.plan_table.bind("<ButtonRelease-1>", self._plan_drag_end, add="+")
        self.plan_dragging = None
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(6, 0))
        for text, command in (("Add combination...", self._add_combination), ("Duplicate", self._duplicate_combination),
                              ("Remove", self._remove_combinations), ("Rename...", self._rename_combination),
                              ("Reset to default", self._reset_plan)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 4))
        self.plan_note_var = tk.StringVar()
        ttk.Label(buttons, textvariable=self.plan_note_var, foreground="#555555").pack(side="left", padx=(8, 0))

    def _show_plan(self, select=()):
        self.plan_table.delete(*self.plan_table.get_children())
        for index, combination in enumerate(self.plan):
            self.plan_table.insert("", "end", iid=str(index), values=(
                index + 1, combination["name"], len(combination["modules"]),
                "\u26a0 yes" if combination["nightmare"] else ""))
        if select:
            self.plan_table.selection_set([str(i) for i in select if i < len(self.plan)])
        self.plan_note_var.set(f"{len(self.plan)} combinations, "
                               f"{sum(c['nightmare'] for c in self.plan)} with nightmares")

    def _plan_selected(self):
        return sorted(int(item) for item in self.plan_table.selection())

    def _plan_drag_start(self, event):
        self.plan_dragging = self.plan_table.identify_row(event.y) or None

    def _plan_drag_motion(self, event):
        if self.plan_dragging is None:
            return
        target = self.plan_table.identify_row(event.y)
        if target and target != self.plan_dragging:
            self.plan_table.move(self.plan_dragging, "", self.plan_table.index(target))

    def _plan_drag_end(self, _event):
        if self.plan_dragging is None:
            return
        moved, self.plan_dragging = self.plan_dragging, None
        order = [int(item) for item in self.plan_table.get_children()]
        if order != sorted(order):
            self.plan = [self.plan[index] for index in order]
            self._show_plan([order.index(int(moved))])

    def _duplicate_combination(self):
        chosen = self._plan_selected()
        for offset, index in enumerate(chosen):
            copy = dict(self.plan[index + offset], name=self.plan[index + offset]["name"] + " (copy)")
            self.plan.insert(index + offset + 1, copy)
        self._show_plan([index + offset + 1 for offset, index in enumerate(chosen)])

    def _remove_combinations(self):
        chosen = set(self._plan_selected())
        self.plan = [c for index, c in enumerate(self.plan) if index not in chosen]
        self._show_plan()

    def _rename_combination(self):
        chosen = self._plan_selected()
        if not chosen:
            return
        index = chosen[0]
        name = self.simpledialog.askstring("Rename combination", "Name:", initialvalue=self.plan[index]["name"],
                                           parent=self.window)
        if name:
            self.plan[index] = dict(self.plan[index], name=name.strip())
            self._show_plan([index])

    def _reset_plan(self):
        if self.messagebox.askyesno("Reset the stress test plan",
                                    "Replace the plan with the built-in baseline? Your combinations are lost when "
                                    "you click Save.", parent=self.window):
            self.plan = stress_test.default_plan()
            self._show_plan()

    def _add_combination(self):
        """Build a combination from runs of the current scenario set and the nightmares, in the order picked."""
        tk, ttk = self.tk, self.ttk
        sources = [(synthetic.run_label(run), run) for run in synthetic.runs(self.scenarios)]
        sources += [(synthetic.run_label(run), run) for run in synthetic.runs(stress_test.load_nightmares())]
        picker = tk.Toplevel(self.window)
        picker.title("Add a combination")
        picker.transient(self.window)
        picker.geometry("900x420")
        frame = ttk.Frame(picker, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Double-click (or Add) runs in the order they should play; each continues from the "
                              "one before. Runs come from the scenario set and the nightmares.",
                  foreground="#555555").pack(anchor="w")
        lists = ttk.Frame(frame)
        lists.pack(fill="both", expand=True, pady=(6, 0))
        available = tk.Listbox(lists, exportselection=False)
        for label, _ in sources:
            available.insert("end", label)
        available.pack(side="left", fill="both", expand=True)
        middle = ttk.Frame(lists)
        middle.pack(side="left", padx=6)
        chosen, chosen_list = [], tk.Listbox(lists, exportselection=False)

        def add(*_):
            for index in available.curselection():
                chosen.append(sources[index][1])
                chosen_list.insert("end", sources[index][0])

        def remove_last():
            if chosen:
                chosen.pop()
                chosen_list.delete("end")

        def create():
            if not chosen:
                return
            modules = [dict(module) for run in chosen for module in run]
            nightmare = any(m["name"].startswith("Nightmare") for m in modules)
            self.plan.append({"name": " -> ".join(synthetic.run_label(run) for run in chosen), "nightmare": nightmare,
                              "modules": stress_test.normalize_plan([{"modules": modules}])[0]["modules"]})
            self._show_plan([len(self.plan) - 1])
            self.plan_table.see(str(len(self.plan) - 1))
            picker.destroy()

        available.bind("<Double-1>", add)
        ttk.Button(middle, text="Add \u2192", command=add).pack(fill="x")
        ttk.Button(middle, text="Remove last", command=remove_last).pack(fill="x", pady=(4, 0))
        chosen_list.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Cancel", command=picker.destroy).pack(side="right")
        ttk.Button(row, text="Create combination", command=create).pack(side="right", padx=(0, 6))
        self.picker = picker  # for tests
        return picker, available, add, create

    # --- scenario sets -------------------------------------------------------------------

    def _refresh_sets(self):
        self.set_picker["values"] = [path.name for path in synthetic.scenario_sets()]

    def _set_path(self):
        return synthetic.SCENARIO_DIR / self.set_var.get()

    def _load_set(self):
        try:
            self.scenarios = synthetic.load_set(self._set_path())
            self.note_var.set("")
        except (OSError, ValueError) as exc:
            self.scenarios = []
            self.note_var.set(f"Can't read {self.set_var.get()}: {exc}")
        self._show()

    def _save_set(self):
        try:
            synthetic.save_set(self.scenarios, self._set_path())
            self.note_var.set(f"Saved {self.set_var.get()}")
        except (OSError, ValueError) as exc:
            self.note_var.set(str(exc))

    def _save_set_as(self):
        name = self.simpledialog.askstring("Save scenario set", "Name for the scenario set:", parent=self.window)
        if not name:
            return
        name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name).strip().replace(" ", "_")
        self.set_var.set(f"{name}.json")
        self._save_set()
        self._refresh_sets()

    # --- the scenario table and editor --------------------------------------------------------

    def _run_labels(self):
        """For each module, its run and step, such as "Run 3 · step 2 of 3"."""
        labels = []
        for number, run in enumerate(synthetic.runs(self.scenarios), start=1):
            for step in range(len(run)):
                labels.append(f"Run {number}" if len(run) == 1 else f"Run {number} \u00b7 step {step + 1} of {len(run)}")
        return labels

    def _show(self, select=None):
        self.table.delete(*self.table.get_children())
        for index, (scenario, label) in enumerate(zip(self.scenarios, self._run_labels())):
            settings = describe(scenario) if not scenario.get("chain") else "continues; " + describe(scenario).split(
                "; ", 1)[-1]
            self.table.insert("", "end", iid=str(index), values=(label, scenario["name"], scenario["type"], settings),
                              tags=("chained",) if scenario.get("chain") else ())
        if self.scenarios:
            chosen = str(min(select if select is not None else 0, len(self.scenarios) - 1))
            self.table.selection_set(chosen)
            self.table.see(chosen)

    def _selected(self):
        selection = self.table.selection()
        return int(selection[0]) if selection else None

    def _select(self):
        index = self._selected()
        if index is None:
            return
        scenario = self.scenarios[index]
        for key, (variable, _) in self.vars.items():
            value = scenario.get(key, "")
            variable.set(f"{value:g}" if isinstance(value, float) else str(value))
        self.chain_var.set(bool(scenario.get("chain")))
        self._enable_fields()

    def _enable_fields(self):
        kind = self.vars["type"][0].get()
        used = set(synthetic.PARAMETERS.get(kind, {}))
        for key, (_, widget) in self.vars.items():
            if key in dict(FIELDS):
                widget.state(["!disabled"] if key in used else ["disabled"])
        self.vars["at"][1].state(["!disabled"] if self.vars["placement"][0].get() == "at" else ["disabled"])

    def _edited(self):
        """The scenario as the editor shows it, checked; raises ScenarioError."""
        kind = self.vars["type"][0].get()
        scenario = {"name": self.vars["name"][0].get().strip(), "type": kind,
                    "placement": self.vars["placement"][0].get(), "at": self.vars["at"][0].get().strip(),
                    "seed": self.vars["seed"][0].get() or 1, "chain": self.chain_var.get()}
        for key in synthetic.PARAMETERS.get(kind, {}):
            text = self.vars[key][0].get().strip()
            if text:
                scenario[key] = text
        try:
            scenario["seed"] = int(scenario["seed"])
        except ValueError:
            raise synthetic.ScenarioError("seed must be a whole number") from None
        return synthetic.normalize(scenario)

    def _apply(self):
        index = self._selected()
        if index is None:
            return
        try:
            self.scenarios[index] = self._edited()
            self.note_var.set("")
        except synthetic.ScenarioError as exc:
            self.note_var.set(str(exc))
            return
        self._show(index)

    def _add(self):
        self.scenarios.append(synthetic.normalize({"name": "New crash", "type": "crash", "placement": "append",
                                                   "seed": len(self.scenarios) + 1}))
        self._show(len(self.scenarios) - 1)

    def _set_chain(self, on):
        index = self._selected()
        if index is None:
            return
        if on and index == 0:
            self.note_var.set("The first module starts a run: there's nothing above it to continue")
            return
        self.scenarios[index] = dict(self.scenarios[index], chain=on)
        self.note_var.set("")
        self._show(index)

    def _drag_start(self, event):
        row = self.table.identify_row(event.y)
        self.dragging = row or None

    def _drag_motion(self, event):
        if self.dragging is None:
            return
        target = self.table.identify_row(event.y)
        if target and target != self.dragging:
            self.table.move(self.dragging, "", self.table.index(target))

    def _drag_end(self, _event):
        """Apply the new row order the drag left in the table."""
        if self.dragging is None:
            return
        moved, self.dragging = self.dragging, None
        order = [int(item) for item in self.table.get_children()]
        if order != sorted(order):
            self.scenarios = [self.scenarios[index] for index in order]
            if self.scenarios and self.scenarios[0].get("chain"):
                self.scenarios[0] = dict(self.scenarios[0], chain=False)  # a run must start somewhere
            self._show(order.index(int(moved)))

    def _duplicate(self):
        index = self._selected()
        if index is not None:
            # The copy follows the original; chained, it joins the same run.
            copy = dict(self.scenarios[index], name=self.scenarios[index]["name"] + " (copy)")
            self.scenarios.insert(index + 1, copy)
            self._show(index + 1)

    def _remove(self):
        index = self._selected()
        if index is not None:
            del self.scenarios[index]
            self._show(index)

    def _move(self, step):
        index = self._selected()
        if index is None or not 0 <= index + step < len(self.scenarios):
            return
        self.scenarios[index], self.scenarios[index + step] = self.scenarios[index + step], self.scenarios[index]
        if self.scenarios[0].get("chain"):
            self.scenarios[0] = dict(self.scenarios[0], chain=False)  # a run must start somewhere
        self._show(index + step)

    # --- preview ------------------------------------------------------------------------

    def _preview(self):
        from backtest import DEFAULT_CACHE_DIR, GRANULARITY_SECONDS, load_cached_candles
        index = self._selected()
        if index is None or not self.market_var.get():
            return
        try:
            self.scenarios[index] = self._edited()  # preview what the editor shows
        except synthetic.ScenarioError as exc:
            self.note_var.set(str(exc))
            return
        run = next(run for run in synthetic.runs(self.scenarios) if any(m is self.scenarios[index] for m in run))
        self._show(index)
        product, granularity = self.market_var.get().split()
        loaded = load_cached_candles(DEFAULT_CACHE_DIR, product, granularity, read_only=True)
        if not loaded:
            self.note_var.set(f"No cached {product} {granularity} candles")
            return
        candles, seconds = list(loaded[0]), GRANULARITY_SECONDS[granularity]
        self.ax.clear()
        try:
            for path in range(min(synthetic.run_paths(run), 10)):
                built, start = synthetic.build(candles, run, seconds, path)
                shown = [c for c in built if c["start"] >= start - 365 * 86400]
                times = [datetime.fromtimestamp(c["start"], timezone.utc) for c in shown]
                self.ax.plot(times, [c["close"] for c in shown], linewidth=1, alpha=0.8)
            history = [c for c in candles if c["start"] >= start - 365 * 86400]
            self.ax.plot([datetime.fromtimestamp(c["start"], timezone.utc) for c in history],
                         [c["close"] for c in history], color="#555555", linewidth=1.2, label="history")
            self.ax.axvline(datetime.fromtimestamp(start, timezone.utc), color="#b3261e", linewidth=1, linestyle="--")
            self.ax.set_yscale("log")
            self.ax.set_title(f"{synthetic.run_label(run)} on {product} {granularity} (red line: the run starts)",
                              fontsize=9)
            self.note_var.set("")
        except synthetic.ScenarioError as exc:
            self.note_var.set(str(exc))
        self.canvas.draw()

    def _save(self):
        try:
            top_n = max(1, int(self.top_var.get()))
        except ValueError:
            self.note_var.set("The number of models to highlight must be a whole number")
            return
        try:
            stress_paths = max(1, int(self.paths_var.get()))
        except ValueError:
            self.note_var.set("The stress test's paths must be a whole number")
            return
        if self.mode_var.get() == "stress" and not self.plan:
            self.plan_note_var.set("Add at least one combination, or reset the plan to the default")
            self.tabs.select(1)
            return
        try:
            stress_test.save_plan(self.plan)
        except (OSError, ValueError) as exc:
            self.plan_note_var.set(f"Plan not saved: {exc}")
            return
        self.options = {"mode": self.mode_var.get(), "scenario_set": str(self._set_path()), "top_n": top_n,
                        "stress_paths": stress_paths}
        if self.options["mode"] in ("historic+synthetic", "synthetic"):
            if not self.scenarios:
                self.note_var.set("Add at least one scenario, or test on historic data only")
                return
            self._save_set()
        save_options(self.options)
        self.on_save(self.options)
        self.window.destroy()

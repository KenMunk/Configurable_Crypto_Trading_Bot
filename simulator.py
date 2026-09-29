import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from backtest import (
    LOOKBACK_YEARS,
    load_candles,
    lookback_window,
    resolve_granularity,
    flash_crash_levels_of,
    flash_crash_reserve_of,
    run_backtest,
    starting_capital_of,
)
from friction import FrictionModel, fetch_friction_inputs
from model_config import CompiledModel, ProfileError, is_legacy_profile
from engine import QuantitativeTradingEngine, ensure_coinbase_sdk, load_environment_config

try:
    import tkinter as tk
    from tkinter import ttk, messagebox

    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure

    from chart import BacktestChart, ModelValueChart
except ImportError:  # pragma: no cover - GUI is optional in headless env
    tk = None
    ttk = None
    messagebox = None


class SimulationProfile:
    @staticmethod
    def from_json(path):
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload


def _format_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_model(indicators, rules):
    """The profile's indicators and signal rules, as report lines."""
    lines = ["", "Indicators:"]
    lines += [f"- {meta['label']}: {meta['summary']}" + (
        f" (outputs {', '.join(meta['outputs'])})" if len(meta["outputs"]) > 1 else "") for meta in indicators]
    if not indicators:
        lines.append("- none")
    for label, all_key, any_key in (("Entry", "entry_all", "entry_any"), ("Exit", "exit_all", "exit_any")):
        lines.append(f"{label} signal:")
        lines += [f"- all of: {rule}" for rule in rules[all_key]]
        lines += [f"- any of: {rule}" for rule in rules[any_key]]
        if not rules[all_key] and not rules[any_key]:
            lines.append("- no rules (exits only through the hard stop, profit lock or drawdown limit)")
    return lines


def describe_profile(profile):
    """Validate a profile; return (is_valid, report text) for the Load Profile button."""
    try:
        _, granularity_seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        model = CompiledModel(profile, granularity_seconds)
        starting_capital = starting_capital_of(profile)
        flash_levels = flash_crash_levels_of(profile)
        flash_reserve = flash_crash_reserve_of(profile)
    except ProfileError as exc:
        return False, "The profile has problems:\n" + "\n".join(f"- {error}" for error in exc.errors)
    except ValueError as exc:
        return False, f"The profile has problems:\n- {exc}"
    notes = []
    if is_legacy_profile(profile):
        notes = ["", "This profile uses the original fixed EMA fields; they were translated into the "
                     "indicators and rules above. See Project_Documentation/indicator_reference.md to "
                     "rewrite it in the current format."]
    source = ("capital_allocation.starting_capital" if "starting_capital" in profile.get("capital_allocation", {})
              else "default; set capital_allocation.starting_capital to change it")
    if not flash_levels:
        flash = "off"
    elif flash_reserve:
        flash = f"{flash_reserve:,.2f} reserve: " + ", ".join(
            f"{flash_reserve * allocation:,.2f} at {drop:.0%} below" for drop, allocation in flash_levels)
    else:
        flash = ", ".join(f"{allocation:.0%} of cash at {drop:.0%} below" for drop, allocation in flash_levels)
    lines = (["Profile is valid.", "", f"Starting value: {starting_capital:,.2f} ({source})",
              f"Flash-crash buys: {flash}"]
             + format_model(model.indicator_meta(), model.rule_text()) + notes)
    return True, "\n".join(lines)


def format_fee_line(friction):
    """One-line summary of the fee rates a run is using."""
    line = (f"taker {friction['taker_rate']:.2%} / maker {friction['maker_rate']:.2%} "
            f"({friction['commission_source']})")
    if friction["network_fee_btc"]:
        line += f"; BTC network fee {friction['network_fee_btc']:.8f} BTC"
    return line


def format_friction(result):
    friction = result["friction"]
    lines = [
        "",
        "Friction model:",
        f"- Exchange fees: {format_fee_line(friction)}",
        f"- Charged per fill: {friction['fill_fee_rate']:.3%} commission + {friction['slippage']:.2%} slippage",
        f"- Liquidity buffer (screen only): {friction['liquidity_fee_buffer']:.0%} of round-trip fees"
        f" + network fee ({friction['network_fee_detail']})",
        f"- Tax buffer: {friction['tax_rate']:.1%} of each year's net realized gain",
    ]
    if friction["screen_enabled"]:
        expected = friction.get("expected_net_return")
        verdict = ""
        if expected is not None:
            verdict = (f"; a full-size first trade reaching the target nets {expected:+.2%} after friction and tax, "
                       f"which {'passes' if expected >= friction['minimum_net_profit'] else 'fails'}")
        lines.append(
            f"- Friction screen: enter only if the {friction['profit_target']:.1%} profit target nets at least "
            f"{friction['minimum_net_profit']:.1%} after friction and tax{verdict}"
        )
        lines.append(f"- Entry signals blocked by the screen: {result['screened_signals']:,}")
    else:
        lines.append("- Friction screen: off (needs profit_lock_target and minimum_net_profit_gate)")

    lines += ["", f"Taxes paid: {result['taxes_paid']:,.2f}"]
    for payment in result["tax_payments"]:
        if payment["tax"] or payment["net_realized_gain"]:
            lines.append(
                f"- {payment['year']}: net realized {payment['net_realized_gain']:+,.2f}, "
                f"tax {payment['tax']:,.2f}, loss carried forward {payment['loss_carryforward']:,.2f}"
            )
    lines.append(f"Estimated tax due for the unfinished final year: {result['estimated_tax_due']:,.2f}")
    return lines


def format_backtest_result(result, recent_trades=10):
    """Render a backtest result as text for the metrics panel."""
    lines = [
        f"Product: {result['product_id']}",
        f"Period: {_format_time(result['period_start'])} to {_format_time(result['period_end'])}",
        *format_test_start(result),
        f"Candles simulated: {result['candle_count']:,}",
        "",
        f"Starting capital: {result['starting_capital']:,.2f}",
        f"Ending equity: {result['ending_equity']:,.2f}",
        f"Total return: {result['total_return']:+.2%}",
        f"Buy and hold return: {result['buy_and_hold_return']:+.2%}",
        f"Max drawdown: {result['max_drawdown']:.2%} (tax payments count as withdrawals, not losses)",
        f"Worst drawdown within any {result['drawdown_window']}: {result['max_window_drawdown']:.2%}",
        f"Trades: {result['trade_count']:,} (win rate {result['win_rate']:.1%})",
        f"Fees paid: {result['fees_paid']:,.2f} at {result['commission_rate']:.3%} per fill",
        f"Position open at end: {'yes' if result['open_position'] else 'no'}",
    ]
    lines += format_model(result["indicators"], result["rules"])
    lines += format_friction(result)
    events = result.get("flash_crash_events", [])
    if events:
        lines += ["", f"Flash-crash events: {len(events):,}"]
        for event in events:
            lasted = "never recovered" if event["minutes"] is None else f"recovered after {event['minutes']:,.0f} min"
            handling = "ignored as bad data" if event["brief"] else "held through"
            lines.append(f"- {_format_time(event['start_time'])}: from {event['reference']:,.2f} down to "
                         f"{event['low']:,.2f}, {lasted}; {handling}")
    flash = result["flash_crash_buys"]
    if flash:
        lines += ["", f"Flash-crash buys: {len(flash):,} fills, net {sum(t['pnl'] for t in flash):+,.2f}"]
        lines += [f"- {_format_time(t['entry_time'])}: {t['entry_reason']} bought at {t['entry_price']:,.2f}, "
                  f"sold at {t['exit_price']:,.2f} ({t['pnl']:+,.2f})" for t in flash[-10:]]
    pauses = result["drawdown_pauses"]
    if pauses:
        lines.append(f"Drawdown limit pauses: {len(pauses):,}")
        for pause in pauses[-recent_trades:]:
            resumed = _format_time(pause["resumed_at"]) if pause["resumed_at"] else "still paused at end"
            lines.append(f"- paused {_format_time(pause['paused_at'])}, resumed {resumed}")

    trades = result["trades"][-recent_trades:]
    if trades:
        lines += ["", f"Last {len(trades)} trades:"]
        for trade in trades:
            lines.append(
                f"- {_format_time(trade['entry_time'])} -> {_format_time(trade['exit_time'])}: "
                f"{trade['entry_price']:,.2f} -> {trade['exit_price']:,.2f} "
                f"({trade['return']:+.2%}, {trade['exit_reason']})"
            )

    lines += ["", "Not modeled yet: " + ", ".join(result["unmodeled_settings"])]
    return "\n".join(lines)


# (key, heading, width in pixels, anchor) for each Trades table column.
TRADE_COLUMNS = [
    ("number", "#", 50, "e"),
    ("entry_time", "Entry (UTC)", 130, "w"),
    ("entry_price", "Entry price", 95, "e"),
    ("exit_time", "Exit (UTC)", 130, "w"),
    ("exit_price", "Exit price", 95, "e"),
    ("held", "Held", 75, "e"),
    ("position_cost", "Position", 90, "e"),
    ("fees", "Fees", 75, "e"),
    ("pnl", "P&L", 90, "e"),
    ("return", "Return", 75, "e"),
    ("outcome", "Outcome", 70, "w"),
    ("value_after", "Value after", 95, "e"),
    ("exit_reason", "Exit reason", 120, "w"),
]


def _format_duration(seconds):
    days, remainder = divmod(int(seconds), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _short_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M")


def trade_rows(trades):
    """Return display text and sort keys for each trade, in trade order."""
    rows = []
    for number, trade in enumerate(trades, start=1):
        held = trade["exit_time"] - trade["entry_time"]
        outcome = "Win" if trade["pnl"] > 0 else "Loss"
        rows.append({
            "trade": trade,
            "display": {
                "number": f"{number:,}",
                "entry_time": _short_time(trade["entry_time"]),
                "entry_price": f"{trade['entry_price']:,.2f}",
                "exit_time": _short_time(trade["exit_time"]),
                "exit_price": f"{trade['exit_price']:,.2f}",
                "held": _format_duration(held),
                "position_cost": f"{trade['position_cost']:,.2f}",
                "pnl": f"{trade['pnl']:+,.2f}",
                "return": f"{trade['return']:+.2%}",
                "outcome": outcome,
                "value_after": f"{trade['equity_after']:,.2f}",
                "fees": f"{trade['fees']:,.2f}",
                "exit_reason": trade["exit_reason"],
            },
            "sort": {
                "number": number,
                "entry_time": trade["entry_time"],
                "entry_price": trade["entry_price"],
                "exit_time": trade["exit_time"],
                "exit_price": trade["exit_price"],
                "held": held,
                "position_cost": trade["position_cost"],
                "pnl": trade["pnl"],
                "return": trade["return"],
                "outcome": outcome,
                "value_after": trade["equity_after"],
                "fees": trade["fees"],
                "exit_reason": trade["exit_reason"],
            },
        })
    return rows


def trade_summary(trades):
    """Summarize trade outcomes in two lines for the Trades tab."""
    if not trades:
        return "No trades were made in this simulation"
    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    returns = [trade["return"] for trade in trades]
    exits = {}
    for trade in trades:
        exits[trade["exit_reason"]] = exits.get(trade["exit_reason"], 0) + 1
    return (
        f"{len(trades):,} trades: {wins:,} wins, {len(trades) - wins:,} losses "
        f"({wins / len(trades):.1%} win rate). Net P&L {sum(t['pnl'] for t in trades):+,.2f}. "
        f"Average return {sum(returns) / len(returns):+.2%}, best {max(returns):+.2%}, "
        f"worst {min(returns):+.2%}.\n"
        "Exits: " + ", ".join(f"{reason} {count:,}" for reason, count in
                              sorted(exits.items(), key=lambda item: -item[1]))
    )


CONFIG_DIRECTORY = Path(__file__).parent / "config"

# (key, heading, width in pixels, anchor) for each Comparison table column.
COMPARISON_COLUMNS = [
    ("model", "Model", 200, "w"),
    ("product", "Product", 80, "w"),
    ("candles", "Candles", 130, "w"),
    ("start", "Start", 90, "e"),
    ("end", "End", 110, "e"),
    ("return", "Return", 90, "e"),
    ("buy_and_hold", "Buy & hold", 90, "e"),
    ("max_drawdown", "Max drawdown", 110, "e"),
    ("window_drawdown", "Worst in window", 125, "e"),
    ("flash", "Flash buys", 80, "e"),
    ("trades", "Trades", 60, "e"),
    ("win_rate", "Win rate", 70, "e"),
    ("fees", "Fees", 90, "e"),
    ("taxes", "Taxes", 90, "e"),
    ("blocked", "Blocked signals", 105, "e"),
]


def parse_start_date(text, now=None):
    """The test start as a unix timestamp, or None when blank.

    Accepts "YYYY-MM-DD" or "YYYY-MM-DD HH:MM", in UTC.
    """
    text = (text or "").strip()
    if not text:
        return None
    for layout in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            moment = datetime.strptime(text, layout).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Start date {text!r} must look like 2020-01-01 or 2020-01-01 14:30 (UTC)")
    if moment > (now or datetime.now(timezone.utc)):
        raise ValueError(f"Start date {text} is in the future")
    return int(moment.timestamp())


def format_test_start(result):
    """Report lines describing where the test started and how the first entry was found."""
    if result.get("requested_start") is None:
        return []
    lines = [f"Test start: {_format_time(result['period_start'])}"]
    if result["requested_start"] < result["period_start"] - 86_400:
        lines[0] += (f" (the requested {_format_time(result['requested_start'])} is before the earliest "
                     f"available candle)")
    warmup_days = result["warmup_candles"] * (result["series"]["time"][1] - result["series"]["time"][0]
                                              if len(result["series"]["time"]) > 1 else 0) / 86_400
    lines.append(f"Indicator warm-up: {warmup_days:,.0f} days of history before the start")
    how = "a fresh entry signal" if result["waited_for_fresh_signal"] else "its entry rules to hold"
    if result["first_entry"] is None:
        lines.append(f"First entry: none; the model waited for {how} and it never came")
    else:
        waited = (result["first_entry"] - result["period_start"]) / 86_400
        lines.append(f"First entry: {_format_time(result['first_entry'])}, {waited:,.1f} days after the start "
                     f"(waited for {how})")
    return lines


def model_names(paths):
    """Display names for profile files: the file name, made unique when two collide."""
    names, seen = [], {}
    for path in paths:
        name = Path(path).stem
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return names


def comparison_rows(results):
    """Display text and sort keys for each model's headline results."""
    rows = []
    for result in results:
        values = {
            "model": result.get("model_name", result["product_id"]),
            "product": result["product_id"],
            "candles": result.get("granularity", ""),
            "start": result["starting_capital"],
            "end": result["ending_equity"],
            "return": result["total_return"],
            "buy_and_hold": result["buy_and_hold_return"],
            "max_drawdown": result["max_drawdown"],
            "window_drawdown": result["max_window_drawdown"],
            "flash": len(result["flash_crash_buys"]),
            "trades": result["trade_count"],
            "win_rate": result["win_rate"],
            "fees": result["fees_paid"],
            "taxes": result["taxes_paid"],
            "blocked": result["screened_signals"],
        }
        display = dict(values)
        for key in ("start", "end", "fees", "taxes"):
            display[key] = f"{values[key]:,.2f}"
        for key in ("return", "buy_and_hold"):
            display[key] = f"{values[key]:+.2%}"
        display["max_drawdown"] = f"{values['max_drawdown']:.1%}"
        display["window_drawdown"] = f"{values['window_drawdown']:.2%} / {result['drawdown_window']}"
        display["flash"] = f"{values['flash']:,}"
        display["win_rate"] = f"{values['win_rate']:.1%}"
        display["trades"] = f"{values['trades']:,}"
        display["blocked"] = f"{values['blocked']:,}"
        rows.append({"result": result, "display": display, "sort": values})
    return rows


def group_by_market(models):
    """Group (name, profile) pairs by the candle history they need: (product, granularity)."""
    groups = {}
    for name, profile in models:
        granularity, _ = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        groups.setdefault((profile["product_id"], granularity), []).append(name)
    return groups


class SimulationWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("Quant Bot Simulator")
        self.root.geometry("1200x900")
        self.root.minsize(900, 700)

        self.model_paths = []
        self.results = []
        self.result_var = tk.StringVar(value="Simulation status: idle")
        self.auth_status_var = tk.StringVar(value="Coinbase authentication: not tested")
        self.runtime_config = load_environment_config()
        self.auth_engine = QuantitativeTradingEngine(
            base_directory=".", node_is_primary=False, env_file=".env"
        )
        # The worker thread reports through this queue; only the Tk thread touches widgets.
        self.progress_queue = queue.Queue()
        self.simulation_thread = None

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Models (select one or more; Ctrl/Shift-click for several)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        models_row = ttk.Frame(main)
        models_row.pack(fill="x", pady=(6, 12))
        self.model_list = tk.Listbox(models_row, selectmode="extended", height=6, exportselection=False,
                                     activestyle="none")
        list_scroll = ttk.Scrollbar(models_row, orient="vertical", command=self.model_list.yview)
        self.model_list.configure(yscrollcommand=list_scroll.set)
        self.model_list.pack(side="left", fill="x", expand=True)
        list_scroll.pack(side="left", fill="y")
        model_buttons = ttk.Frame(models_row)
        model_buttons.pack(side="left", padx=(8, 0), anchor="n")
        ttk.Button(model_buttons, text="Add file...", command=self._browse_config).pack(fill="x")
        ttk.Button(model_buttons, text="Select all", command=lambda: self.model_list.select_set(0, "end")).pack(
            fill="x", pady=(4, 0))
        ttk.Button(model_buttons, text="Refresh", command=self._refresh_models).pack(fill="x", pady=(4, 0))
        self._refresh_models()

        start_row = ttk.Frame(main)
        start_row.pack(fill="x", pady=(0, 10))
        ttk.Label(start_row, text="Start testing from (UTC, optional):").pack(side="left")
        self.start_date_var = tk.StringVar(value="")
        ttk.Entry(start_row, textvariable=self.start_date_var, width=18).pack(side="left", padx=(6, 0))
        self.fresh_signal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(start_row, text="Wait for a fresh entry signal", variable=self.fresh_signal_var).pack(
            side="left", padx=(12, 0))
        ttk.Label(start_row, text="e.g. 2020-01-01. Blank tests all 10 years. The earlier history still warms up "
                                  "the indicators.", foreground="#555555").pack(side="left", padx=(12, 0))

        action_row = ttk.Frame(main)
        action_row.pack(fill="x", pady=(0, 12))
        ttk.Button(action_row, text="Load Profile", command=self._load_profile).pack(side="left")
        self.run_button = ttk.Button(action_row, text="Run Simulation", command=self._run_simulation)
        self.run_button.pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Test Coinbase Authentication", command=self._authenticate_coinbase).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(main, text="Progress", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", pady=(6, 12))
        progress_frame.columnconfigure(1, weight=1)
        self.progress_bars = {}
        self.progress_vars = {}
        for row, (phase, label) in enumerate(
            [
                ("api", "API check"),
                ("data", f"Data gathering ({LOOKBACK_YEARS} years)"),
                ("simulation", "Simulation"),
            ]
        ):
            ttk.Label(progress_frame, text=label, width=24).grid(row=row * 2, column=0, sticky="w")
            bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
            bar.grid(row=row * 2, column=1, sticky="ew")
            detail = tk.StringVar(value="waiting")
            ttk.Label(progress_frame, textvariable=detail, foreground="#555555").grid(
                row=row * 2 + 1, column=1, sticky="w", pady=(0, 6)
            )
            self.progress_bars[phase] = bar
            self.progress_vars[phase] = detail

        # Status is packed from the bottom first so the expanding tabs never push it off-screen.
        ttk.Label(main, textvariable=self.auth_status_var, foreground="#1a5d1a").pack(
            side="bottom", anchor="w", pady=(4, 0)
        )
        ttk.Label(main, textvariable=self.result_var, foreground="#1a5d1a").pack(
            side="bottom", anchor="w", pady=(4, 0)
        )
        ttk.Label(main, text="Status", font=("Segoe UI", 10, "bold")).pack(side="bottom", anchor="w")

        picker_row = ttk.Frame(main)
        picker_row.pack(fill="x", pady=(0, 6))
        ttk.Label(picker_row, text="Showing model:").pack(side="left")
        self.selected_model_var = tk.StringVar(value="")
        self.model_picker = ttk.Combobox(picker_row, textvariable=self.selected_model_var, state="readonly", width=40)
        self.model_picker.pack(side="left", padx=(6, 0))
        self.model_picker.bind("<<ComboboxSelected>>", lambda _event: self._show_model(self.model_picker.current()))
        ttk.Label(picker_row, text="(Chart, Trades and Metrics follow this choice; Comparison and Model value show all)",
                  foreground="#555555").pack(side="left", padx=(8, 0))

        self.results_tabs = ttk.Notebook(main)
        self.results_tabs.pack(fill="both", expand=True, pady=(0, 10))

        self.comparison_tab = self._add_comparison_tab()
        self.chart_tab, self.chart = self._add_chart_tab("Chart", BacktestChart)
        self.value_tab, self.value_chart = self._add_chart_tab("Model value", ModelValueChart)
        self.trades_tab = self._add_trades_tab()

        self.metrics_tab = ttk.Frame(self.results_tabs)
        self.results_tabs.add(self.metrics_tab, text="Metrics")
        self.metrics_text = tk.Text(self.metrics_tab, wrap="word", bg="#f5f5f5")
        self.metrics_text.pack(fill="both", expand=True)

    def _add_chart_tab(self, title, chart_class):
        tab = ttk.Frame(self.results_tabs)
        self.results_tabs.add(tab, text=title)
        figure = Figure(figsize=(9, 5), dpi=100)
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab, pack_toolbar=False)
        toolbar.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return tab, chart_class(figure, canvas)

    def _add_comparison_tab(self):
        tab = ttk.Frame(self.results_tabs, padding=(0, 6, 0, 0))
        self.results_tabs.add(tab, text="Comparison")
        self.comparison_summary_var = tk.StringVar(value="Run a simulation to compare the selected models")
        ttk.Label(tab, textvariable=self.comparison_summary_var).pack(anchor="w", pady=(0, 6))
        ttk.Label(tab, text="Click a column heading to sort. Double-click a model to open its chart.",
                  foreground="#555555").pack(anchor="w", pady=(0, 6))
        table_frame = ttk.Frame(tab)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.comparison_table = ttk.Treeview(
            table_frame, columns=[key for key, *_ in COMPARISON_COLUMNS], show="headings", selectmode="browse"
        )
        for key, heading, width, anchor in COMPARISON_COLUMNS:
            self.comparison_table.heading(key, text=heading, command=lambda key=key: self._sort_comparison(key))
            self.comparison_table.column(key, width=width, anchor=anchor, stretch=key == "model")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.comparison_table.xview)
        self.comparison_table.configure(xscrollcommand=horizontal.set)
        self.comparison_table.grid(row=0, column=0, sticky="nsew")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.comparison_table.bind("<Double-1>", self._open_compared_model)
        self.comparison_rows = {}
        self.comparison_sort = ("return", True)
        return tab

    def _show_comparison(self, results):
        self.comparison_table.delete(*self.comparison_table.get_children())
        self.comparison_rows = {}
        for row in comparison_rows(results):
            item = self.comparison_table.insert("", "end",
                                                values=[row["display"][key] for key, *_ in COMPARISON_COLUMNS])
            self.comparison_rows[item] = row
        self.comparison_sort = ("return", False)
        self._sort_comparison("return")  # best first
        best = max(results, key=lambda result: result["total_return"])
        started = results[0].get("requested_start")
        self.comparison_summary_var.set(
            (f"Test started {_format_time(results[0]['period_start'])}. " if started is not None else "")
            + f"{len(results)} models compared. Best: {best['model_name']}, "
            f"{best['starting_capital']:,.0f} to {best['ending_equity']:,.0f} ({best['total_return']:+.2%})."
        )

    def _sort_comparison(self, key):
        sort_key, descending = self.comparison_sort
        descending = not descending if key == sort_key else key not in ("model", "product", "candles")
        items = sorted(self.comparison_rows, key=lambda item: self.comparison_rows[item]["sort"][key],
                       reverse=descending)
        for position, item in enumerate(items):
            self.comparison_table.move(item, "", position)
        self.comparison_sort = (key, descending)

    def _open_compared_model(self, _event):
        selection = self.comparison_table.selection()
        if selection:
            result = self.comparison_rows[selection[0]]["result"]
            self._show_model(self.results.index(result))
            self.results_tabs.select(self.chart_tab)

    def _show_model(self, index):
        """Point the Chart, Trades and Metrics tabs, and the Model value highlight, at one model."""
        if not 0 <= index < len(self.results):
            return
        result = self.results[index]
        self.model_picker.current(index)
        self.metrics_text.delete("1.0", tk.END)
        self.metrics_text.insert("1.0", f"Model: {result['model_name']}\n\n" + format_backtest_result(result))
        self.chart.show(result)
        self.value_chart.show(self.results, primary=index)
        self._show_trades(result)

    def _refresh_models(self):
        """List every profile in config/, keeping files added from elsewhere and the selection."""
        selected = set(self.selected_model_paths()) if self.model_paths else None
        found = sorted(str(path) for path in CONFIG_DIRECTORY.glob("*.json"))
        added = [path for path in self.model_paths if path not in found and Path(path).exists()]
        self.model_paths = found + added
        self.model_list.delete(0, "end")
        for name in model_names(self.model_paths):
            self.model_list.insert("end", name)
        for index, path in enumerate(self.model_paths):
            default = Path(path).stem == "btc_macro_horizon"
            if (selected is not None and path in selected) or (selected is None and default):
                self.model_list.select_set(index)
        if not self.model_list.curselection() and self.model_paths:
            self.model_list.select_set(0)

    def selected_model_paths(self):
        return [self.model_paths[index] for index in self.model_list.curselection()]

    def select_models(self, paths):
        """Select the given profile files, adding any that aren't listed yet."""
        paths = [str(Path(path).resolve()) for path in paths]
        known = [str(Path(path).resolve()) for path in self.model_paths]
        for path in paths:
            if path not in known:
                self.model_paths.append(path)
                known.append(path)
                self.model_list.insert("end", model_names(self.model_paths)[-1])
        self.model_list.select_clear(0, "end")
        for path in paths:
            self.model_list.select_set(known.index(path))

    def _selected_models(self):
        """(name, profile) for each selected file; raises if nothing is selected."""
        indices = self.model_list.curselection()
        if not indices:
            raise ValueError("Select at least one model in the Models list")
        names = model_names(self.model_paths)
        return [(names[index], SimulationProfile.from_json(self.model_paths[index])) for index in indices]

    def _add_trades_tab(self):
        tab = ttk.Frame(self.results_tabs, padding=(0, 6, 0, 0))
        self.results_tabs.add(tab, text="Trades")
        self.trade_summary_var = tk.StringVar(value="Run a simulation to see its trades")
        ttk.Label(tab, textvariable=self.trade_summary_var, justify="left").pack(anchor="w", pady=(0, 6))
        ttk.Label(
            tab, text="Click a column heading to sort. Double-click a trade to show it on the chart.",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 6))

        table_frame = ttk.Frame(tab)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.trade_table = ttk.Treeview(
            table_frame, columns=[key for key, *_ in TRADE_COLUMNS], show="headings", selectmode="browse"
        )
        for key, heading, width, anchor in TRADE_COLUMNS:
            self.trade_table.heading(key, text=heading, command=lambda key=key: self._sort_trades(key))
            self.trade_table.column(key, width=width, anchor=anchor, stretch=key == "exit_reason")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.trade_table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.trade_table.xview)
        self.trade_table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.trade_table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.trade_table.bind("<Double-1>", self._show_selected_trade)

        self.trade_rows = {}
        self.trade_sort = ("number", False)
        return tab

    def _show_trades(self, result):
        self.trade_table.delete(*self.trade_table.get_children())
        self.trade_rows = {}
        for row in trade_rows(result["trades"]):
            item = self.trade_table.insert("", "end", values=[row["display"][key] for key, *_ in TRADE_COLUMNS])
            self.trade_rows[item] = row
        self.trade_summary_var.set(trade_summary(result["trades"]))
        self.trade_sort = ("number", False)

    def _sort_trades(self, key):
        sort_key, descending = self.trade_sort
        descending = not descending if key == sort_key else False
        items = sorted(self.trade_rows, key=lambda item: self.trade_rows[item]["sort"][key], reverse=descending)
        for position, item in enumerate(items):
            self.trade_table.move(item, "", position)
        self.trade_sort = (key, descending)

    def _show_selected_trade(self, _event):
        selection = self.trade_table.selection()
        if selection:
            self.chart.focus_trade(self.trade_rows[selection[0]]["trade"])
            self.results_tabs.select(self.chart_tab)

    def _authenticate_coinbase(self):
        self.auth_status_var.set("Coinbase authentication: testing")
        approve_path_update = lambda: messagebox.askyesno(
            "Update PATH",
            "The active Python directory is not on PATH. Add it to your user PATH?",
        ) if messagebox is not None else False
        result = self.auth_engine.authenticate_coinbase(
            dependency_prompt=lambda: messagebox.askyesno(
                "Install Coinbase SDK",
                "The Coinbase SDK is missing. Install it now?",
            )
            if messagebox is not None
            else False,
            path_prompt=approve_path_update,
        )
        self.metrics_text.delete("1.0", tk.END)
        self.metrics_text.insert("1.0", json.dumps(result, indent=2))
        self.results_tabs.select(self.metrics_tab)
        if result["authenticated"]:
            account_count = result.get("account_count", "unknown")
            self.auth_status_var.set(f"Coinbase authentication: succeeded ({account_count} accounts)")
        else:
            self.auth_status_var.set(
                f"Coinbase authentication: failed ({result.get('message', 'see engine logs')})"
            )

    def _browse_config(self):
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(
            title="Add model profiles",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            self.select_models(list(self.selected_model_paths()) + list(chosen))

    def _load_profile(self):
        try:
            models = self._selected_models()
            reports, problems = [], []
            for name, profile in models:
                valid, report = describe_profile(profile)
                if not valid:
                    problems.append(name)
                reports.append(f"=== {name} ===\n{report}")
            if len(models) == 1:
                reports.append("Profile JSON:\n" + json.dumps(models[0][1], indent=2))
            self.metrics_text.delete("1.0", tk.END)
            self.metrics_text.insert("1.0", "\n\n".join(reports))
            self.results_tabs.select(self.metrics_tab)
            if problems:
                self.result_var.set(f"Simulation status: problems in {', '.join(problems)} - see the Metrics tab")
            else:
                self.result_var.set(f"Simulation status: loaded {len(models)} valid "
                                    f"{'profile' if len(models) == 1 else 'profiles'}")
        except Exception as exc:
            self.result_var.set(f"Simulation status: error - {exc}")
            if messagebox is not None:
                messagebox.showerror("Simulation Load Error", str(exc))

    def _run_simulation(self):
        if self.simulation_thread is not None and self.simulation_thread.is_alive():
            return
        try:
            models = self._selected_models()
            start_time = parse_start_date(self.start_date_var.get())
            problems = [f"{name}: {report.splitlines()[1][2:]}" for name, profile in models
                        for valid, report in [describe_profile(profile)] if not valid]
            if problems:
                raise ValueError("Fix these profiles first (Load Profile lists every problem):\n" + "\n".join(problems))
            # Created here, not in the worker, because it may prompt with dialogs.
            client = self.auth_engine.create_coinbase_client(
                dependency_prompt=lambda: messagebox.askyesno(
                    "Install Coinbase SDK", "The Coinbase SDK is missing. Install it now?"
                ),
                path_prompt=lambda: messagebox.askyesno(
                    "Update PATH", "The active Python directory is not on PATH. Add it to your user PATH?"
                ),
            )
        except Exception as exc:
            self.result_var.set(f"Simulation status: error - {exc}")
            if messagebox is not None:
                messagebox.showerror("Simulation Run Error", str(exc))
            return

        for phase in self.progress_bars:
            self.progress_bars[phase]["value"] = 0
            self.progress_vars[phase].set("waiting")
        self.metrics_text.delete("1.0", tk.END)
        self.run_button.state(["disabled"])
        self.result_var.set("Simulation status: testing the Coinbase API")

        self.simulation_thread = threading.Thread(
            target=self._simulation_worker, args=(client, models, start_time, self.fresh_signal_var.get()),
            daemon=True,
        )
        self.simulation_thread.start()
        self.root.after(100, self._poll_progress)

    def _simulation_worker(self, client, models, start_time=None, wait_for_fresh_signal=False):
        """Test the API, then gather data and simulate each (name, profile) in turn.

        With `start_time`, every model starts testing there (see run_backtest).
        """
        post = self.progress_queue.put
        try:
            post(("progress", "api", 0.0, "testing read-only account access"))
            auth = self.auth_engine.authenticate_coinbase(client=client)
            post(("auth", auth))
            if not auth["authenticated"]:
                post(("error", RuntimeError(f"Coinbase API test failed: {auth['message']}")))
                return

            post(("status", "Simulation status: looking up exchange and network fees"))
            inputs = fetch_friction_inputs(client, [profile for _, profile in models])
            frictions = {name: FrictionModel(profile, inputs) for name, profile in models}
            post(("friction", auth, frictions[models[0][0]].describe()))

            # Models on the same product and candle size share one download.
            markets = list(group_by_market(models))
            candles_by_market = {}
            for number, (product_id, granularity) in enumerate(markets):
                post(("status", f"Simulation status: gathering {product_id} {granularity} data"))
                _, granularity_seconds = resolve_granularity(granularity)
                start, end = lookback_window(granularity_seconds)
                prefix = f"{product_id} {granularity}" + (f" ({number + 1} of {len(markets)})" if len(markets) > 1 else "")
                candles_by_market[(product_id, granularity)] = load_candles(
                    client, product_id, granularity, start, end,
                    progress=lambda fraction, message, number=number, prefix=prefix: post(
                        ("progress", "data", (number + fraction) / len(markets), f"{prefix}: {message}")),
                )

            results = []
            for number, (name, profile) in enumerate(models):
                granularity, _ = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
                candles = candles_by_market[(profile["product_id"], granularity)]
                post(("status", f"Simulation status: simulating {name} ({number + 1} of {len(models)})"))
                prefix = name + (f" ({number + 1} of {len(models)})" if len(models) > 1 else "")
                result = run_backtest(
                    candles, profile, friction=frictions[name],
                    start_time=start_time, wait_for_fresh_signal=wait_for_fresh_signal,
                    progress=lambda fraction, message, number=number, prefix=prefix: post(
                        ("progress", "simulation", (number + fraction) / len(models), f"{prefix}: {message}")),
                )
                result["model_name"] = name
                result["granularity"] = granularity
                results.append(result)
            post(("done", results))
        except Exception as exc:  # Reported in the window; the worker must not die silently.
            post(("error", exc))

    def _poll_progress(self):
        finished = False
        while True:
            try:
                event = self.progress_queue.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                _, phase, fraction, message = event
                self.progress_bars[phase]["value"] = fraction * 100
                self.progress_vars[phase].set(f"{fraction:.0%} - {message}")
            elif kind == "auth":
                auth = event[1]
                if auth["authenticated"]:
                    self.progress_bars["api"]["value"] = 50
                    detail = f"authenticated ({auth['account_count']} accounts), looking up fees"
                    self.auth_status_var.set(f"Coinbase authentication: succeeded ({auth['account_count']} accounts)")
                else:
                    detail = "failed - see status below"
                    self.auth_status_var.set(f"Coinbase authentication: failed ({auth['message']})")
                self.progress_vars["api"].set(detail)
            elif kind == "friction":
                _, auth, friction = event
                self.progress_bars["api"]["value"] = 100
                self.progress_vars["api"].set(
                    f"authenticated ({auth['account_count']} accounts); {format_fee_line(friction)}"
                )
            elif kind == "status":
                self.result_var.set(event[1])
            elif kind == "done":
                self.results = event[1]
                self.model_picker["values"] = [result["model_name"] for result in self.results]
                best = max(range(len(self.results)), key=lambda i: self.results[i]["total_return"])
                self._show_comparison(self.results)
                self._show_model(best)
                if len(self.results) == 1:
                    result = self.results[0]
                    self.results_tabs.select(self.chart_tab)
                    self.result_var.set(f"Simulation status: complete ({result['total_return']:+.2%} over "
                                        f"{result['trade_count']:,} trades)")
                else:
                    self.results_tabs.select(self.comparison_tab)
                    self.result_var.set(
                        f"Simulation status: complete, {len(self.results)} models; best "
                        f"{self.results[best]['model_name']} {self.results[best]['total_return']:+.2%}")
                finished = True
            elif kind == "error":
                self.result_var.set(f"Simulation status: error - {event[1]}")
                if messagebox is not None:
                    messagebox.showerror("Simulation Run Error", str(event[1]))
                finished = True

        if finished:
            self.run_button.state(["!disabled"])
        else:
            self.root.after(100, self._poll_progress)


def main():
    if tk is None:
        raise RuntimeError("tkinter is required for the simulator GUI.")

    root = tk.Tk()
    SimulationWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()

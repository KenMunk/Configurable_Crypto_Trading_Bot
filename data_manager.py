"""Clear the simulator's stored data: the price history (data/candles.db) and the synthetic candles
(data/synthetic/synthetic.db).

Opened from the simulator's "Clear data..." button. The price history is listed one market (product
and candle size) per row; clearing a market deletes its candles and stored indicator columns, so the
next run that needs it downloads it again from Coinbase. Nothing else is touched: model profiles,
the performance log and stress reports stay.
"""

from datetime import datetime, timezone

from backtest import DEFAULT_CACHE_DIR
from candle_store import CandleStore
import synthetic

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # pragma: no cover - GUI is optional in headless env
    tk = ttk = messagebox = None


def format_size(size):
    """Bytes as `12.3 MB`."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024


def _date(timestamp):
    return "" if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def describe_market(market):
    """(market, candle size, candles, from, to, stored indicators) for a row of CandleStore.markets()."""
    product, _, granularity = market["table"].partition("_")
    return (product, granularity, f"{market['candles']:,}", _date(market["first"]), _date(market["last"]),
            str(market["metrics"]))


def history_size(store):
    """The bytes the price history database takes, with its write-ahead log."""
    return sum(path.stat().st_size for path in store.path.parent.glob(store.path.name + "*"))


class ClearDataDialog:
    """Lists the stored markets and synthetic runs, and clears what the user picks after asking.

    `busy()` returning True (a run under way) refuses clearing, as a run reads the data.
    """

    COLUMNS = (("product", "Market", 110), ("granularity", "Candles", 130), ("count", "Count", 90),
               ("first", "From", 95), ("last", "To", 95), ("metrics", "Stored indicators", 120))

    def __init__(self, parent, store=None, synthetic_store=None, busy=lambda: False, ask=None):
        self.store = store or CandleStore(DEFAULT_CACHE_DIR)
        self.synthetic_store = synthetic_store or synthetic.SyntheticStore()
        self.busy = busy
        self.ask = ask or (lambda title, message: messagebox.askyesno(title, message, icon="warning",
                                                                      parent=self.top))
        self.top = tk.Toplevel(parent)
        self.top.title("Clear data")
        self.top.transient(parent)
        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Price history (data/candles.db)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Clearing a market deletes its candles and stored indicators; the next run that "
                              "needs it downloads it again from Coinbase.", foreground="#555555",
                  wraplength=640, justify="left").pack(anchor="w", pady=(2, 6))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=[c for c, _, _ in self.COLUMNS], show="headings",
                                  height=10, selectmode="extended")
        for column, heading, width in self.COLUMNS:
            self.table.heading(column, text=heading, anchor="w")
            self.table.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", lambda event: self._update_buttons())

        self.history_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.history_var, foreground="#555555").pack(anchor="w", pady=(4, 0))
        history_buttons = ttk.Frame(frame)
        history_buttons.pack(fill="x", pady=(6, 12))
        self.clear_selected_button = ttk.Button(history_buttons, text="Clear selected markets",
                                                command=self.clear_selected)
        self.clear_selected_button.pack(side="left")
        self.clear_all_button = ttk.Button(history_buttons, text="Clear all price history", command=self.clear_all)
        self.clear_all_button.pack(side="left", padx=(8, 0))

        ttk.Label(frame, text="Synthetic candles (data/synthetic/synthetic.db)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.synthetic_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.synthetic_var, foreground="#555555").pack(anchor="w", pady=(2, 6))
        synthetic_buttons = ttk.Frame(frame)
        synthetic_buttons.pack(fill="x")
        self.clear_synthetic_button = ttk.Button(synthetic_buttons, text="Clear synthetic candles",
                                                 command=self.clear_synthetic)
        self.clear_synthetic_button.pack(side="left")
        ttk.Button(synthetic_buttons, text="Close", command=self.close).pack(side="right")

        self.status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.status_var, foreground="#1a5d1a").pack(anchor="w", pady=(10, 0))
        self.top.bind("<Escape>", lambda event: self.close())
        self.refresh()

    def refresh(self):
        """Reload the markets and the synthetic store's summary."""
        self.table.delete(*self.table.get_children())
        self.markets = {market["table"]: market for market in self.store.markets()}
        for table, market in self.markets.items():
            self.table.insert("", "end", iid=table, values=describe_market(market))
        candles = sum(market["candles"] for market in self.markets.values())
        self.history_var.set(f"{len(self.markets)} markets, {candles:,} candles, "
                             f"{format_size(history_size(self.store)) if self.store.path.exists() else '0 B'} "
                             f"on disk" if self.markets else "No price history stored")
        runs = self.synthetic_store.runs()
        self.synthetic_runs = len(runs)
        self.synthetic_var.set(f"{len(runs)} stored scenario paths, {format_size(self.synthetic_store.size())}; "
                               "only kept for inspection, nothing reads them back" if runs
                               else "No synthetic candles stored")
        self._update_buttons()

    def _update_buttons(self):
        self.clear_selected_button.state(["!disabled"] if self.table.selection() else ["disabled"])
        self.clear_all_button.state(["!disabled"] if self.markets else ["disabled"])
        self.clear_synthetic_button.state(["!disabled"] if self.synthetic_runs else ["disabled"])

    def _refused(self):
        if self.busy():
            self.status_var.set("A simulation is running; clear data once it has finished")
            return True
        return False

    def _clear(self, tables):
        names = ", ".join(" ".join(describe_market(self.markets[t])[:2]) for t in tables)
        candles = sum(self.markets[table]["candles"] for table in tables)
        if not self.ask("Clear price history",
                        f"Delete {candles:,} candles and their stored indicators for {names}?\n\n"
                        "The next run that needs them downloads them again from Coinbase, which can take "
                        "several minutes for 15-minute candles."):
            return
        cleared = self.store.clear(tables)
        self.refresh()
        self.status_var.set(f"Cleared {len(cleared)} market{'s' if len(cleared) != 1 else ''}: {names}")

    def clear_selected(self):
        if not self._refused() and self.table.selection():
            self._clear(list(self.table.selection()))

    def clear_all(self):
        if not self._refused() and self.markets:
            self._clear(list(self.markets))

    def clear_synthetic(self):
        if self._refused() or not self.synthetic_runs:
            return
        if not self.ask("Clear synthetic candles", f"Delete all {self.synthetic_runs:,} stored scenario paths?"):
            return
        count = self.synthetic_store.clear()
        self.refresh()
        self.status_var.set(f"Cleared {count:,} synthetic scenario paths")

    def close(self):
        self.top.destroy()

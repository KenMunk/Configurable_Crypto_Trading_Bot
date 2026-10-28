"""A horizontal scroll bar and span picker under a chart: show a window of its time axis, such as a year, at a time.

The chart (chart.BacktestChart, chart.ModelValueChart or semi_live.SemiLiveChart) supplies:
    ax                  its main axes, whose time axis every panel shares
    view_range()        (first, last) moment of its data, as matplotlib date numbers (days), or None
    show_window(lo, hi) show [lo, hi], fitting each value axis to the data in it

Times are matplotlib date numbers throughout, so a span is a number of days.
"""

# (label, days); None shows everything. A chart may add spans of its own, such as the semi-live session.
SPANS = [("1 week", 7), ("1 month", 30), ("3 months", 91), ("6 months", 182), ("1 year", 365), ("2 years", 730),
         ("5 years", 1826), ("All", None)]
DEFAULT_SPAN = "1 year"
SCROLL_STEP = 0.1  # of the window, per scroll-bar arrow click or Shift + wheel notch
ZOOM_STEP = 1.25  # per Ctrl + wheel notch
MIN_DAYS = 1 / 24  # an hour


def window_for(first, last, days, end=None):
    """(lo, hi): `days` ending at `end` (the data's end by default), kept within [first, last].

    With `days` None, or longer than the data, the window is all of it.
    """
    if days is None or days >= last - first:
        return first, last
    hi = last if end is None else min(max(end, first + days), last)
    return hi - days, hi


def moved(first, last, lo, hi, start):
    """The window [lo, hi] moved to begin at `start`, kept within [first, last]."""
    days = hi - lo
    start = min(max(start, first), last - days)
    return start, start + days


def fractions(first, last, lo, hi):
    """The scroll bar's (top, bottom) fractions for the window [lo, hi] of [first, last]."""
    total = last - first
    if total <= 0:
        return 0.0, 1.0
    return max(0.0, (lo - first) / total), min(1.0, (hi - first) / total)


def zoomed(first, last, lo, hi, factor, anchor=0.5):
    """The window scaled by `factor` (above 1 shows more), keeping the moment `anchor` of the way across it in
    place; at least MIN_DAYS and at most everything."""
    days = min(max((hi - lo) * factor, MIN_DAYS), last - first)
    pivot = lo + (hi - lo) * anchor
    return moved(first, last, lo, hi, pivot - days * anchor)[0], None if days >= last - first else days


def describe_days(days):
    """A span for the picker, such as "3 months" for a listed one, else "45 days" or "6 hours"."""
    for label, listed in SPANS:
        if listed is not None and abs(listed - days) < 0.01:
            return label
    if days >= 2:
        return f"{days:,.0f} days"
    return f"{days * 24:,.0f} hours"


class TimeWindow:
    """The scroll bar and span picker for one chart, packed into `parent` (above the chart's toolbar).

    `extra_spans` are (label, callable returning (lo, hi) or None) listed first, such as the semi-live
    session; `default` is the span shown at first. The window keeps its place when the chart redraws
    (refresh), following the data's end while it is at the end.
    """

    def __init__(self, parent, chart, widget, default=DEFAULT_SPAN, extra_spans=()):
        import tkinter as tk
        from tkinter import ttk
        self.chart, self.widget = chart, widget
        self.extra = dict(extra_spans)
        self.spans = {**{label: None for label in self.extra}, **dict(SPANS)}
        self.frame = ttk.Frame(parent)
        self.scrollbar = ttk.Scrollbar(self.frame, orient="horizontal", command=self._scroll)
        self.scrollbar.pack(side="left", fill="x", expand=True)
        ttk.Label(self.frame, text="Show:").pack(side="left", padx=(8, 4))
        self.span_var = tk.StringVar(value=default)
        self.picker = picker = ttk.Combobox(self.frame, textvariable=self.span_var, values=list(self.spans),
                                            state="readonly", width=11)
        picker.pack(side="left")
        picker.bind("<<ComboboxSelected>>", lambda _event: self.choose(self.span_var.get()))
        ttk.Label(self.frame, text="Ctrl + wheel zooms, Shift + wheel scrolls", foreground="#555555").pack(
            side="left", padx=(8, 0))
        self.days = self.spans.get(default)
        self.window = None  # (lo, hi) on show
        self.at_end = True  # the window follows the data's end as it grows
        self._watched = None  # the axes whose time axis is being followed
        widget.bind("<Control-MouseWheel>", self._wheel_zoom)
        widget.bind("<Shift-MouseWheel>", self._wheel_scroll)

    def add_span(self, label, window):
        """List a span of the chart's own, first: `window` returns its (lo, hi), or None when there is none."""
        if label not in self.extra:
            self.extra[label] = window
            self.spans = {label: None, **self.spans}
            self.picker.configure(values=list(self.spans))

    def choose(self, label):
        """Show the span called `label`, ending where the window ends now."""
        self.span_var.set(label)
        if label in self.extra:
            window = self.extra[label]()
            if window is not None:
                self.days = window[1] - window[0]
                return self._show(*window)
            label = DEFAULT_SPAN  # nothing to show for it now, such as no session on this chart
            self.span_var.set(label)
        self.days = self.spans.get(label)
        bounds = self.chart.view_range()
        if bounds is None:
            return
        end = None if self.at_end or self.window is None else self.window[1]
        self._show(*window_for(*bounds, self.days, end))

    def reset(self, label):
        """Start over at the span `label` the next time the chart is shown (refresh), as for new data."""
        self.span_var.set(label)
        self.days = self.spans.get(label)
        self.window, self.at_end = None, True

    def refresh(self):
        """Re-apply the window after the chart redrew: at the data's end if it was there, else where it was."""
        bounds = self.chart.view_range()
        if bounds is None:
            return
        if self.window is None and self.span_var.get() in self.extra:
            return self.choose(self.span_var.get())
        if self.window is None or self.at_end:
            return self._show(*window_for(*bounds, self.days))
        lo, hi = self.window
        days = min(hi - lo, bounds[1] - bounds[0])
        self._show(*moved(*bounds, lo, lo + days, lo))

    def _show(self, lo, hi):
        bounds = self.chart.view_range()
        self.window = (lo, hi)
        self.at_end = bounds is None or hi >= bounds[1] - 1e-9
        self.chart.show_window(lo, hi)
        self._watch()
        if bounds is not None:
            self.scrollbar.set(*fractions(*bounds, lo, hi))

    def _watch(self):
        """Follow the chart's own pan and zoom (the toolbar, a trade double-clicked) with the scroll bar."""
        ax = self.chart.ax
        if ax is not self._watched:
            self._watched = ax
            ax.callbacks.connect("xlim_changed", self._limits_changed)

    def _limits_changed(self, ax):
        bounds = self.chart.view_range()
        if bounds is None:
            return
        lo, hi = ax.get_xlim()
        self.window = (lo, hi)
        self.days = None if hi - lo >= bounds[1] - bounds[0] else hi - lo
        self.at_end = hi >= bounds[1] - 1e-9
        self.scrollbar.set(*fractions(*bounds, lo, hi))

    def _scroll(self, *args):
        """The scroll bar's command: ("moveto", fraction) or ("scroll", count, "units" or "pages")."""
        bounds = self.chart.view_range()
        if bounds is None or self.window is None:
            return
        first, last = bounds
        lo, hi = self.window
        if args[0] == "moveto":
            start = first + float(args[1]) * (last - first)
        else:
            step = SCROLL_STEP if args[2] == "units" else 0.9
            start = lo + int(args[1]) * step * (hi - lo)
        self._show(*moved(first, last, lo, hi, start))

    def _wheel_scroll(self, event):
        self._scroll("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    def _wheel_zoom(self, event):
        bounds = self.chart.view_range()
        if bounds is None or self.window is None:
            return "break"
        anchor = min(max(event.x / max(self.widget.winfo_width(), 1), 0.0), 1.0)
        start, days = zoomed(*bounds, *self.window, 1 / ZOOM_STEP if event.delta > 0 else ZOOM_STEP, anchor)
        self.days = days
        lo, hi = (start, start + days) if days is not None else bounds
        self.span_var.set("All" if days is None else describe_days(days))
        self._show(lo, hi)
        return "break"

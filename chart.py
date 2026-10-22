"""Backtest charts: price with its indicators, oscillator panels and trades, and model value
over time against buy and hold.

The charts draw on any matplotlib canvas, so the simulator embeds them in Tk and
the tests render them headless. Hover shows a crosshair and tooltip, redrawn by
blitting so moving the mouse doesn't re-render hundreds of thousands of points.
"""

from datetime import datetime, timezone

import matplotlib.dates as mdates
import numpy as np
from matplotlib.ticker import FuncFormatter, Locator, LogLocator, MaxNLocator, NullFormatter

SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
BORDER = "#e1e0d9"

PRICE_COLOR = "#2a78d6"
# Price-scale indicators take the remaining categorical slots in fixed order. Green and
# red are left out because buy and sell markers use them. Past five indicators the hues
# repeat with a dashed line, so every indicator keeps a distinct color-and-style pair.
OVERLAY_COLORS = ["#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"]
OVERLAY_DASHES = [None, (0, (5, 2)), (0, (1.5, 1.5))]
# Lines within one oscillator panel, in fixed order (e.g. MACD line, signal, histogram).
PANEL_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
PRICE_PANEL_HEIGHT = 3  # relative to each oscillator panel
MODEL_VALUE_COLOR = "#2a78d6"
# Models compared on the Model value chart take the categorical slots in fixed order, so a
# model keeps its color between runs as long as its place in the list is unchanged.
MODEL_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BENCHMARK_COLOR = SECONDARY_INK  # buy and hold is context, so it recedes
PAUSE_SHADE = "#ebeae5"  # neutral gray wash for drawdown-limit pauses
# Market regimes (profiles with "regimes"): each takes a categorical hue in list order, shown as a faint wash over
# the chart and a solid strip along its top edge; the base regime gets no wash and a gray strip.
REGIME_COLORS = ["#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"]
REGIME_BASE_COLOR = BASELINE
REGIME_WASH_ALPHA = 0.08
REGIME_STRIP = (0.965, 1.0)  # the strip's bottom and top, as fractions of the chart's height
REGIME_NAME_MIN_WIDTH = 0.04  # a stretch narrower than this share of the chart is too narrow to name in the strip
BUY_COLOR = "#008300"
SELL_COLOR = "#e34948"
LINE_WIDTH = 1.5  # points; about 2px at 100 dpi


def _format_value(value):
    if np.isnan(value):
        return "warming up"
    return f"{value:,.2f}" if abs(value) >= 1 or value == 0 else f"{value:.4f}"


def _price_formatter(value, _position):
    return f"{value:,.0f}" if value >= 100 else f"{value:,.2f}"


def _format_timestamp(timestamp):
    return f"{datetime.fromtimestamp(int(timestamp), timezone.utc):%Y-%m-%d %H:%M} UTC"


def _quote_currency(result):
    return str(result.get("product_id") or "").partition("-")[2] or "quote currency"


def _day(timestamp):
    return f"{datetime.fromtimestamp(int(timestamp), timezone.utc):%Y-%m-%d}"


def regime_color(number):
    """The color of the regime at `number` in result["regimes"]["names"] (0 is the base regime)."""
    return REGIME_BASE_COLOR if number == 0 else REGIME_COLORS[(number - 1) % len(REGIME_COLORS)]


def _ink_on(color):
    """Dark ink on a light background color, light ink on a dark one."""
    red, green, blue = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return PRIMARY_INK if 0.299 * red + 0.587 * green + 0.114 * blue > 0.6 else SURFACE


def regime_runs(result):
    """[(regime number, first index, last index)] for each stretch of candles in one regime, from the first candle
    the model is ready to trade (regimes can't be told apart while the detectors warm up), or [] without regimes."""
    active = result["series"].get("regime")
    if not active or not result.get("regimes"):
        return []
    ready = result.get("ready_to_trade")
    start = int(np.searchsorted(np.asarray(result["series"]["time"]), ready)) if ready is not None else len(active)
    runs = []
    for index in range(start + 1, len(active) + 1):
        if index == len(active) or active[index] != active[start]:
            runs.append((active[start], start, index - 1))
            start = index
    return runs


def regime_at(result, index):
    """The name of the regime active at candle `index` ("warming up" before the model is ready to trade), or None
    without regimes."""
    active = result["series"].get("regime")
    if not active or not result.get("regimes"):
        return None
    ready = result.get("ready_to_trade")
    if ready is None or result["series"]["time"][min(index, len(active) - 1)] < ready:
        return "warming up"
    return result["regimes"]["names"][active[min(index, len(active) - 1)]]


def buy_and_hold_values(result):
    """The value of buying at the benchmark's start and holding: the starting capital until then."""
    closes = np.asarray(result["series"]["close"], dtype=float)
    times = np.asarray(result["series"]["time"])
    start = int(np.searchsorted(times, result.get("buy_and_hold_start", times[0])))
    values = result["starting_capital"] * closes / closes[start]
    values[:start] = result["starting_capital"]
    return values


# Share of the price chart's height kept free at the bottom for the buy and sell triangles.
TRADE_STRIP = 0.09


def _reserve_trade_strip(ax):
    """Lower the price axis's bottom so the data fills the top and the trade triangles sit in an empty strip
    below it, clear of every line."""
    low, high = ax.get_ylim()
    if ax.get_yscale() == "log" and low > 0:
        ax.set_ylim(low / (high / low) ** (TRADE_STRIP / (1 - TRADE_STRIP)), high)
    else:
        ax.set_ylim(low - (high - low) * TRADE_STRIP / (1 - TRADE_STRIP), high)


def _trade_marks(ax, entries, exits, markersize, line_alpha, zorder, labels=False):
    """Mark buys and sells with a vertical line through the chart and a triangle at its bottom edge.

    The triangles sit in the margin under the price lines (in axes coordinates, so zooming and
    panning keep them there) instead of on the price, where they hid the lines. Buys sit a little
    lower than sells, so a sell and a buy on the same candle both show. Returns the artists.
    """
    edge = ax.get_xaxis_transform()  # x in data (time), y from 0 (bottom) to 1 (top) of the axes; see TRADE_STRIP
    style = dict(linestyle="none", markersize=markersize, markeredgecolor=SURFACE, markeredgewidth=1.5,
                 transform=edge, clip_on=False, zorder=zorder)
    return [
        ax.vlines(entries, 0, 1, transform=edge, colors=BUY_COLOR, linewidth=0.8, alpha=line_alpha, zorder=zorder - 1),
        ax.vlines(exits, 0, 1, transform=edge, colors=SELL_COLOR, linewidth=0.8, alpha=line_alpha, zorder=zorder - 1),
        *ax.plot(entries, np.full(len(entries), 0.025), marker="^", color=BUY_COLOR,
                 label="Buy" if labels else "_nolegend_", **style),
        *ax.plot(exits, np.full(len(exits), 0.065), marker="v", color=SELL_COLOR,
                 label="Sell" if labels else "_nolegend_", **style),
    ]


class PriceLogLocator(Locator):
    """Ticks for a log price axis that stay readable at every zoom level.

    Plain log ticks land only on 1-9 x 10^n, so zooming inside one decade leaves one
    or two labels. Within a decade this switches to evenly spaced round prices.
    """

    def __call__(self):
        return self.tick_values(*self.axis.get_view_interval())

    def tick_values(self, vmin, vmax):
        vmin, vmax = sorted((vmin, vmax))
        vmin = max(vmin, 1e-9)
        ratio = vmax / vmin
        if ratio < 10:
            ticks = MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]).tick_values(vmin, vmax)
            return ticks[ticks > 0]
        subs = (1.0, 2.0, 5.0) if ratio < 1000 else (1.0,)
        return LogLocator(subs=subs, numticks=30).tick_values(vmin, vmax)


class _HoverChart:
    """Shared axes styling, log value axis, and blitted crosshair tooltip."""

    placeholder = "Run a simulation to see the chart"

    def __init__(self, figure, canvas):
        self.figure = figure
        self.canvas = canvas
        self.figure.set_facecolor(SURFACE)
        # Constrained layout keeps stacked panels, titles and tick labels from overlapping.
        self.figure.set_layout_engine("constrained", h_pad=0.02, hspace=0.02)
        self.ax = figure.add_subplot(111)
        self.axes = [self.ax]
        self.background = None
        self.result = None
        self.x_values = None
        self.hover_values = None
        self._hover_artists = []
        self.floor = None
        self.floor_fits = True
        self._shown = None  # the arguments of the last show(), so a new floor can redraw it
        canvas.mpl_connect("draw_event", self._on_draw)
        canvas.mpl_connect("motion_notify_event", self._on_move)
        canvas.mpl_connect("figure_leave_event", lambda event: self._hide_hover())
        self._style_axes()
        self.ax.text(0.5, 0.5, self.placeholder, transform=self.ax.transAxes,
                     ha="center", va="center", color=MUTED_INK)
        self.canvas.draw_idle()

    def _style_axes(self, ax=None):
        ax = ax or self.ax
        ax.set_facecolor(SURFACE)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(BASELINE)
        ax.tick_params(colors=MUTED_INK, labelcolor=SECONDARY_INK, labelsize=9)
        ax.grid(axis="y", which="major", color=GRIDLINE, linewidth=0.8, linestyle="-")
        ax.set_axisbelow(True)

    def _panel_count(self, result):
        """How many stacked panels sit under the main chart; none by default."""
        return 0

    def set_floor(self, floor):
        """Start the main chart's value axis at `floor`, or fit it to the data when None.

        The floor outlasts redraws for other models and runs. Setting it redraws the full
        view, which becomes the toolbar's Home. Returns False when the floor is at or above
        every value on the chart; the chart then shows all its values instead.
        """
        self.floor = floor
        if self._shown is not None:
            self.show(*self._shown)
        return self.floor_fits

    def _begin(self, result):
        self.result = result
        self.figure.clear()
        panels = self._panel_count(result)
        grid = self.figure.add_gridspec(1 + panels, 1, height_ratios=[PRICE_PANEL_HEIGHT] + [1] * panels)
        self.ax = self.figure.add_subplot(grid[0])
        # Panels share the main chart's time axis, so zooming or panning any of them moves all.
        self.axes = [self.ax] + [self.figure.add_subplot(grid[i], sharex=self.ax) for i in range(1, 1 + panels)]
        for ax in self.axes:
            self._style_axes(ax)
        times = np.array(result["series"]["time"], dtype="datetime64[s]")
        self.x_values = mdates.date2num(times)
        return times

    def _shade_pauses(self):
        series_end = self.result["series"]["time"][-1]
        for number, pause in enumerate(self.result.get("drawdown_pauses", [])):
            resumed = pause["resumed_at"] or series_end
            for ax in self.axes:
                ax.axvspan(np.datetime64(pause["paused_at"], "s"), np.datetime64(resumed, "s"),
                           color=PAUSE_SHADE, linewidth=0, zorder=0,
                           label="Paused (drawdown limit)" if number == 0 and ax is self.ax else "_nolegend_")
        halted = self.result.get("halted_at")
        if halted is not None and halted <= series_end:
            for ax in self.axes:
                ax.axvspan(np.datetime64(halted, "s"), np.datetime64(series_end, "s"), color=PAUSE_SHADE,
                           linewidth=0, zorder=0, label="Halted (loss halt)" if ax is self.ax else "_nolegend_")

    def _shade_regimes(self):
        """Mark which regime is active: a strip along the main chart's top in each regime's color, named where a
        stretch is wide enough; a faint wash of that color over every panel (none for base); and a dashed line
        at each switch. Nothing is marked while the detectors warm up."""
        runs = regime_runs(self.result)
        if not runs:
            return
        names = self.result["regimes"]["names"]
        times = np.array(self.result["series"]["time"], dtype="datetime64[s]")
        span = max(float((times[-1] - times[0]).astype(np.int64)), 1.0)
        labelled = set()
        for position, (number, first, last) in enumerate(runs):
            start = times[first]
            end = times[last + 1] if last + 1 < len(times) else times[last]
            color = regime_color(number)
            label = f"Regime: {names[number]}" if number not in labelled else "_nolegend_"
            labelled.add(number)
            if number:
                for ax in self.axes:
                    ax.axvspan(start, end, color=color, alpha=REGIME_WASH_ALPHA, linewidth=0, zorder=0)
            self.ax.axvspan(start, end, ymin=REGIME_STRIP[0], ymax=REGIME_STRIP[1], color=color, linewidth=0,
                            zorder=6, label=label)
            if float((end - start).astype(np.int64)) / span >= REGIME_NAME_MIN_WIDTH:
                self.ax.text(start + (end - start) / 2, sum(REGIME_STRIP) / 2, names[number],
                             transform=self.ax.get_xaxis_transform(), ha="center", va="center", fontsize=7.5,
                             color=_ink_on(color), zorder=7, clip_on=True)
            if position:
                for ax in self.axes:
                    ax.axvline(start, color=color, linestyle=(0, (3, 3)), linewidth=1, alpha=0.8, zorder=1)

    def _finish(self, title, subtitle, ylabel, hover_values, hover_color, legend_columns, legend_location="upper left"):
        """Axes, titles, legend and floor shared by both charts, then the hidden hover artists."""
        ax = self.ax
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(PriceLogLocator())
        ax.yaxis.set_major_formatter(FuncFormatter(_price_formatter))
        ax.yaxis.set_minor_formatter(NullFormatter())
        # Only the bottom panel labels the shared time axis.
        bottom = self.axes[-1]
        locator = mdates.AutoDateLocator()
        bottom.xaxis.set_major_locator(locator)
        bottom.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        for upper in self.axes[:-1]:
            upper.tick_params(labelbottom=False)
        ax.margins(x=0.01)

        ax.set_ylabel(ylabel, color=SECONDARY_INK, fontsize=9)
        ax.set_title(title, loc="left", color=PRIMARY_INK, fontsize=11)
        ax.set_title(subtitle, loc="right", color=SECONDARY_INK, fontsize=9)
        top = REGIME_STRIP[0] - 0.01 if regime_runs(self.result) else 1.0  # keep clear of the regime strip
        legend = ax.legend(loc=legend_location, frameon=False, fontsize=9, ncols=legend_columns,
                           bbox_to_anchor=(0, 0, 1, top))
        for text in legend.get_texts():
            text.set_color(SECONDARY_INK)

        self.floor_fits = True
        if self.floor is not None:
            top = ax.get_ylim()[1]
            self.floor_fits = bool(self.floor < top)
            if self.floor_fits:
                ax.set_ylim(self.floor, top)

        self.hover_values = hover_values
        # One crosshair line per panel, so the hovered moment lines up across all of them.
        self.crosshairs = [panel.axvline(self.x_values[0], color=BASELINE, linewidth=1,
                                         animated=True, visible=False) for panel in self.axes]
        (self.hover_dot,) = ax.plot([], [], marker="o", markersize=8, color=hover_color,
                                    markeredgecolor=SURFACE, markeredgewidth=2,
                                    animated=True, visible=False)
        # Pinned to a top corner of the main chart rather than following the cursor, so a
        # long list of indicator values never runs off the edge.
        self.tooltip_top = top - 0.02
        self.tooltip = ax.annotate(
            "", xy=(0.99, self.tooltip_top), xycoords="axes fraction", ha="right", va="top",
            fontsize=9, color=PRIMARY_INK, animated=True, visible=False, zorder=10,
            bbox=dict(boxstyle="round,pad=0.6", facecolor=SURFACE, edgecolor=BORDER),
        )
        self._hover_artists = [*self.crosshairs, self.hover_dot, self.tooltip]
        for artist in self._hover_artists:
            artist.set_in_layout(False)  # a visible tooltip must never resize the panels
        toolbar = getattr(self.canvas, "toolbar", None)
        if toolbar is not None:
            toolbar.update()  # Home must return to this chart's view, not the previous chart's
        self.canvas.draw_idle()

    def tooltip_text(self, index):
        """Hover text for the candle at `index`; each chart supplies its own."""
        raise NotImplementedError

    def _on_draw(self, _event):
        self.background = self.canvas.copy_from_bbox(self.figure.bbox)
        self._blit()

    def _on_move(self, event):
        if self.result is None or self.background is None:
            return
        toolbar = getattr(self.canvas, "toolbar", None)
        panning_or_zooming = toolbar is not None and bool(toolbar.mode)
        if event.inaxes not in self.axes or event.xdata is None or panning_or_zooming:
            self._hide_hover()
            return

        index = int(np.clip(np.searchsorted(self.x_values, event.xdata), 1, len(self.x_values) - 1))
        if event.xdata - self.x_values[index - 1] < self.x_values[index] - event.xdata:
            index -= 1
        x, y = self.x_values[index], self.hover_values[index]

        for crosshair in self.crosshairs:
            crosshair.set_xdata([x, x])
        self.hover_dot.set_data([x], [y])
        self.tooltip.set_text(self.tooltip_text(index))
        # Sit in the corner away from the cursor so the tooltip never hides the hovered point.
        cursor_on_right = event.x > self.ax.bbox.x0 + self.ax.bbox.width / 2
        self.tooltip.xy = (0.01, self.tooltip_top) if cursor_on_right else (0.99, self.tooltip_top)
        self.tooltip.set_horizontalalignment("left" if cursor_on_right else "right")
        for artist in self._hover_artists:
            artist.set_visible(True)
        self._blit()

    def _hide_hover(self):
        if not any(artist.get_visible() for artist in self._hover_artists):
            return
        for artist in self._hover_artists:
            artist.set_visible(False)
        self._blit()

    def _blit(self):
        if self.background is None:
            return
        self.canvas.restore_region(self.background)
        for artist in self._hover_artists:
            if artist.get_visible():
                artist.axes.draw_artist(artist)
        self.canvas.blit(self.figure.bbox)


class BacktestChart(_HoverChart):
    """Close price with every price-scale indicator and each trade's entry and exit, and a
    panel under it for each oscillator."""

    def _panel_count(self, result):
        return len([meta for meta in result["indicators"] if meta["panel_outputs"]])

    def show(self, result):
        self._shown = (result,)
        times = self._begin(result)
        self._focus_artists = []  # cleared along with the axes
        series = result["series"]
        ax = self.ax
        self.closes = np.asarray(series["close"])
        self.indicators = {name: np.asarray(values) for name, values in series["indicators"].items()}

        ax.plot(times, self.closes, color=PRICE_COLOR, linewidth=LINE_WIDTH,
                label="Close", solid_joinstyle="round", zorder=2)
        self.plotted = [meta for meta in result["indicators"] if meta["price_scale_outputs"]]
        # Short-period lines hug the price, so they sit beneath it; slower, more telling
        # lines draw on top of it.
        by_period = sorted(self.plotted, key=lambda meta: meta.get("period") or 0)
        for position, meta in enumerate(self.plotted):
            color = OVERLAY_COLORS[position % len(OVERLAY_COLORS)]
            dashes = OVERLAY_DASHES[(position // len(OVERLAY_COLORS)) % len(OVERLAY_DASHES)]
            period = meta.get("period") or 0
            zorder = 1 if period and period < 10 else 3 + by_period.index(meta)
            for number, output in enumerate(meta["price_scale_outputs"]):
                band_edge = output.endswith((".upper", ".lower"))
                line_style = {"linestyle": dashes} if dashes else {}
                ax.plot(times, self.indicators[output], color=color, **line_style,
                        linewidth=1 if band_edge else LINE_WIDTH, solid_joinstyle="round", zorder=zorder,
                        label=meta["label"] if number == 0 else "_nolegend_")

        self.panels = [meta for meta in result["indicators"] if meta["panel_outputs"]]
        for panel_ax, meta in zip(self.axes[1:], self.panels):
            self._draw_panel(panel_ax, times, meta)

        trades = result["trades"]
        if trades:
            # Hundreds of trades at full zoom would carpet the chart; shrink to the ~8px floor.
            dense = len(trades) > 200
            entries = np.array([t["entry_time"] for t in trades], dtype="datetime64[s]")
            exits = np.array([t["exit_time"] for t in trades], dtype="datetime64[s]")
            _trade_marks(ax, entries, exits, markersize=6 if dense else 9, line_alpha=0.15 if dense else 0.35,
                         zorder=5, labels=True)

        self._shade_pauses()
        self._shade_regimes()
        subtitle = f"{result['trade_count']:,} trades, {result['total_return']:+.2%} return"
        self._finish(
            title=f"{result['product_id']} close price, indicators and trades",
            subtitle=subtitle,
            ylabel=f"Price ({_quote_currency(result)}, log scale)",
            hover_values=self.closes,
            hover_color=PRICE_COLOR,
            legend_columns=3,
        )
        if trades and (self.floor is None or not self.floor_fits):  # a floor in use is the lowest value shown
            _reserve_trade_strip(ax)

    def _draw_panel(self, ax, times, meta):
        """One oscillator on its own scale, with its conventional reference levels."""
        for level in meta["levels"]:
            ax.axhline(level, color=BASELINE, linewidth=1, zorder=1)
        outputs = meta["panel_outputs"]
        for output, color in zip(outputs, PANEL_COLORS * len(outputs)):
            field = output.split(".", 1)[1] if "." in output else meta["label"]
            ax.plot(times, self.indicators[output], color=color, linewidth=1.2, zorder=2,
                    solid_joinstyle="round", label=field)
        if meta["bounds"]:
            # Fixed-scale oscillators label their bounds and reference levels (0, 30, 70, 100).
            ax.set_ylim(*meta["bounds"])
            ax.set_yticks(sorted(set(meta["levels"]) | set(meta["bounds"])))
        else:
            ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.4g}"))
        ax.text(0.005, 0.95, meta["label"], transform=ax.transAxes, ha="left", va="top",
                fontsize=9, color=SECONDARY_INK, zorder=4,
                bbox=dict(boxstyle="round,pad=0.2", facecolor=SURFACE, edgecolor="none", alpha=0.85))
        if len(outputs) > 1:
            legend = ax.legend(loc="upper right", frameon=False, fontsize=8, ncols=len(outputs))
            for text in legend.get_texts():
                text.set_color(SECONDARY_INK)

    def tooltip_text(self, index):
        """Every indicator's value at the candle, oscillators included."""
        lines = [_format_timestamp(self.result["series"]["time"][index]),
                 f"Close  {self.closes[index]:,.2f}"]
        regime = regime_at(self.result, index)
        if regime is not None:
            lines.append(f"Regime  {regime}")
        for meta in self.result["indicators"]:
            if len(meta["outputs"]) == 1:
                lines.append(f"{meta['label']}  {_format_value(self.indicators[meta['outputs'][0]][index])}")
            else:
                values = ", ".join(f"{output.split('.', 1)[1]} {_format_value(self.indicators[output][index])}"
                                   for output in meta["outputs"])
                lines.append(f"{meta['label']}  {values}")
        return "\n".join(lines)

    def focus_trade(self, trade):
        """Zoom to one trade with some context on each side; the toolbar's Back undoes it."""
        if self.result is None:
            return
        toolbar = getattr(self.canvas, "toolbar", None)
        if toolbar is not None:
            toolbar.push_current()
        times = np.asarray(self.result["series"]["time"])
        duration = max(trade["exit_time"] - trade["entry_time"], 3600)
        start = trade["entry_time"] - duration
        end = trade["exit_time"] + duration
        visible = (times >= start) & (times <= end)
        prices = np.concatenate([
            self.closes[visible],
            *(self.indicators[output][visible] for meta in self.plotted for output in meta["price_scale_outputs"]),
            [trade["entry_price"], trade["exit_price"]],
        ])
        low, high = np.nanmin(prices), np.nanmax(prices)
        padding = (high / low) ** 0.08
        bottom = low / padding
        # The floor still cuts off lines below it, but never the trade's own fills.
        if self.floor is not None and self.floor < min(trade["entry_price"], trade["exit_price"]):
            bottom = max(bottom, self.floor)

        # Mark which trade this is: a wash over the holding period and full-size markers.
        for artist in self._focus_artists:
            artist.remove()
        entry_x, exit_x = (np.datetime64(int(trade[key]), "s") for key in ("entry_time", "exit_time"))
        self._focus_artists = [
            self.ax.axvspan(entry_x, exit_x, color=PRICE_COLOR, alpha=0.1, linewidth=0, zorder=0),
            *_trade_marks(self.ax, np.array([entry_x]), np.array([exit_x]), markersize=11, line_alpha=0.8, zorder=6),
        ]
        self.ax.set_xlim(mdates.date2num(np.datetime64(int(start), "s")),
                         mdates.date2num(np.datetime64(int(end), "s")))
        self.ax.set_ylim(bottom, high * padding)
        if bottom != self.floor:  # unless the floor set the bottom
            _reserve_trade_strip(self.ax)
        # Fit each unbounded oscillator panel to its values in the zoomed window.
        for panel_ax, meta in zip(self.axes[1:], self.panels):
            if meta["bounds"]:
                continue
            values = np.concatenate([self.indicators[output][visible] for output in meta["panel_outputs"]])
            if np.isnan(values).all():
                continue
            panel_low, panel_high = np.nanmin(values), np.nanmax(values)
            margin = (panel_high - panel_low) * 0.1 or abs(panel_high) * 0.1 or 1.0
            panel_ax.set_ylim(panel_low - margin, panel_high + margin)
        if toolbar is not None:
            toolbar.push_current()
        self.canvas.draw_idle()


class ModelValueChart(_HoverChart):
    """Portfolio value over time for one or more models, against buying and holding.

    Set `tax_panel` False to leave out the tax reserve panel and give the value chart the height.
    """

    tax_panel = True

    def _panel_count(self, result):
        return 1 if self.tax_panel else 0

    def show(self, result, primary=0, slots=None, heading=None, marks=(), legend_location=None):
        """Plot one backtest result, or a list of them to compare models on one chart.

        With several results, `primary` picks the one the hover dot follows and that is
        drawn slightly heavier. Colors are tied to each model's position in the list, or to
        its entry in `slots`, so a model keeps its color when only some models are shown.
        `heading` replaces the chart's title; `marks` are (unix time, label) moments drawn as
        dashed vertical lines, such as where a scenario starts. `legend_location` overrides where the
        legend goes (a matplotlib location such as "best").
        """
        self._shown = (result, primary, slots)
        runs = result if isinstance(result, list) else [result]
        single = len(runs) == 1
        primary = min(primary, len(runs) - 1)
        slots = list(slots) if slots is not None else list(range(len(runs)))
        self.runs = runs
        times = self._begin(runs[primary])
        ax = self.ax
        self.run_times = [np.array(run["series"]["time"], dtype="datetime64[s]") for run in runs]
        self.run_x = [mdates.date2num(run_times) for run_times in self.run_times]
        self.run_values = [np.asarray(run["series"]["equity"]) for run in runs]
        self.run_tax_reserves = [
            np.asarray(run["series"].get("tax_reserve", np.zeros_like(run["series"]["equity"])))
            for run in runs
        ]

        # One buy-and-hold line per product and starting value. Each starts where a model could
        # first buy: the chosen model's start for its own line, else the first model with that product and value.
        self.benchmarks = {}
        for position in [primary] + [p for p in range(len(runs)) if p != primary]:
            run = runs[position]
            key = (run["product_id"], run["starting_capital"])
            if key not in self.benchmarks:
                self.benchmarks[key] = (position, buy_and_hold_values(run))
        for capital in sorted({run["starting_capital"] for run in runs}):
            ax.axhline(capital, color=MUTED_INK, linewidth=1, zorder=1, label=f"Starting capital ({capital:,.0f})")
        for (product, capital), (position, values) in self.benchmarks.items():
            run = runs[position]
            start = run.get("buy_and_hold_start", run["series"]["time"][0])
            label = "Buy and hold" if len(self.benchmarks) == 1 else f"Buy and hold {product} ({capital:,.0f})"
            label += f" from {_day(start)}"
            if not single:
                label += f" ({run.get('model_name', f'model {position + 1}')}'s start)"
            ax.plot(self.run_times[position], values, color=BENCHMARK_COLOR, linewidth=1, zorder=2, label=label)
            # An open circle marks where buy and hold starts, since every model's start can differ.
            at = int(np.searchsorted(np.asarray(run["series"]["time"]), start))
            ax.plot([self.run_times[position][at]], [values[at]], marker="o", markersize=7, color=BENCHMARK_COLOR,
                    markerfacecolor=SURFACE, markeredgewidth=1.5, linestyle="none", zorder=5, label="_nolegend_")

        for position, run in enumerate(runs):
            values = self.run_values[position]
            color = MODEL_COLORS[slots[position] % len(MODEL_COLORS)]
            dashes = OVERLAY_DASHES[(slots[position] // len(MODEL_COLORS)) % len(OVERLAY_DASHES)]
            label = ("Model value" if single else
                     f"{run.get('model_name', f'Model {position + 1}')}: {values[-1]:,.0f} ({run['total_return']:+.0%})")
            ax.plot(self.run_times[position], values, color=color, **({"linestyle": dashes} if dashes else {}),
                    linewidth=LINE_WIDTH if position == primary else 1.2, zorder=4 if position == primary else 3,
                    label=label, solid_joinstyle="round")
            # Every model's ending value gets a marker; a single model also gets text labels.
            ax.plot([self.run_times[position][-1]], [values[-1]], marker="o", markersize=8, color=color,
                    markeredgecolor=SURFACE, markeredgewidth=2, zorder=5, label="_nolegend_")
            if not self.tax_panel:
                continue
            tax_label = ("Tax reserve" if single else run.get("model_name", f"Model {position + 1}"))
            tax_label += f" ({_quote_currency(run)})"
            self.axes[1].plot(
                self.run_times[position], self.run_tax_reserves[position], color=color,
                **({"linestyle": dashes} if dashes else {}),
                linewidth=LINE_WIDTH if position == primary else 1.2, label=tax_label,
            )

        for moment, mark_label in marks:
            for panel in self.axes:
                panel.axvline(np.datetime64(int(moment), "s"), color=SECONDARY_INK, linestyle=(0, (4, 3)),
                              linewidth=1, zorder=2, label=mark_label if panel is ax else "_nolegend_")

        primary_run = runs[primary]
        self.model_values = self.run_values[primary]
        self.tax_reserve_values = self.run_tax_reserves[primary]
        self.tax_reserve_ax = self.axes[1] if self.tax_panel else None
        if self.tax_panel:
            tax_currencies = {_quote_currency(run) for run in runs}
            tax_axis_label = (f"Tax reserve ({next(iter(tax_currencies))})" if len(tax_currencies) == 1
                              else "Tax reserve (mixed quote currencies)")
            self.tax_reserve_ax.set_ylabel(tax_axis_label,
                                           color=SECONDARY_INK, fontsize=9)
            self.tax_reserve_ax.set_ylim(bottom=0)
            self.tax_reserve_ax.legend(loc="upper left", frameon=False, fontsize=8, ncols=2)
        self.benchmark_values = self.benchmarks[(primary_run["product_id"], primary_run["starting_capital"])][1]
        capitals = {run["starting_capital"] for run in runs}
        start_value = self.model_values[0]
        ax.plot([times[0]], [start_value], marker="o", markersize=8, color=MODEL_VALUE_COLOR,
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=5, label="_nolegend_")
        if len(capitals) == 1:
            ax.annotate(f"Start {start_value:,.0f}", xy=(times[0], start_value), xytext=(6, 10),
                        textcoords="offset points", ha="left", va="center", fontsize=9, color=PRIMARY_INK, zorder=5)
        if single:
            self._shade_pauses()
            self._shade_regimes()
            ax.annotate(f"End {self.model_values[-1]:,.0f}", xy=(times[-1], self.model_values[-1]), xytext=(6, 0),
                        textcoords="offset points", ha="left", va="center", fontsize=9, color=PRIMARY_INK, zorder=5)
            title = f"{primary_run['product_id']} model value over time"
            bought = primary_run.get("buy_and_hold_start", primary_run["series"]["time"][0])
            subtitle = (f"Started at {start_value:,.2f}; model {primary_run['total_return']:+.2%}, "
                        f"buy and hold {primary_run['buy_and_hold_return']:+.2%} from {_day(bought)}")
        else:
            products = ", ".join(sorted({run["product_id"] for run in runs}))
            best = max(runs, key=lambda run: run["total_return"])
            title = f"{products} model value over time: {len(runs)} models"
            subtitle = (f"Started at {', '.join(f'{c:,.0f}' for c in sorted(capitals))}; best "
                        f"{best.get('model_name', 'model')} {best['total_return']:+.2%}")

        self._finish(
            title=heading or title,
            subtitle=subtitle,
            ylabel=f"Value ({_quote_currency(primary_run)}, log scale)",
            hover_values=self.model_values,
            hover_color=MODEL_COLORS[slots[primary] % len(MODEL_COLORS)],
            legend_columns=2,
            # Several model lines fill the top of the chart; the lower right stays clear.
            legend_location=legend_location or ("upper left" if single else "lower right"),
        )

    def tooltip_text(self, index):
        moment = self.x_values[index]
        lines = [_format_timestamp(self.result["series"]["time"][index])]
        for position, run in enumerate(self.runs):
            # Runs can have different candle times, so each is read at its first candle at or after the hovered one.
            nearest = int(np.clip(np.searchsorted(self.run_x[position], moment), 0, len(self.run_x[position]) - 1))
            value, capital = self.run_values[position][nearest], run["starting_capital"]
            name = "Model value" if len(self.runs) == 1 else run.get("model_name", f"Model {position + 1}")
            lines.append(f"{name}  {value:,.2f} ({value / capital - 1:+.2%})")
            regime = regime_at(run, nearest)
            if regime is not None:
                lines.append(f"{'Regime' if len(self.runs) == 1 else f'{name} regime'}  {regime}")
            tax_reserve = self.run_tax_reserves[position][nearest]
            tax_name = "Tax reserve" if len(self.runs) == 1 else f"{name} tax reserve"
            tax_name += f" ({_quote_currency(run)})"
            lines.append(f"{tax_name}  {tax_reserve:,.2f}")
        for (product, capital), (position, values) in self.benchmarks.items():
            nearest = int(np.clip(np.searchsorted(self.run_x[position], moment), 0, len(values) - 1))
            name = "Buy and hold" if len(self.benchmarks) == 1 else f"Buy and hold {product}"
            lines.append(f"{name}  {values[nearest]:,.2f} ({values[nearest] / capital - 1:+.2%})")
        return "\n".join(lines)

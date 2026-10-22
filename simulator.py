"""Tk simulator: backtest one or more model profiles on Coinbase history and compare them.

The report and table helpers here are plain functions so the tests can check them
without a display. Each run also updates the model performance CSV log.
"""

import json
import math
import queue
import threading
import time
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
from model_library import discard_folders, profile_paths, read_best, read_profile, update_best
from memory_guard import (DEFAULT_PERCENT as DEFAULT_MEMORY_PERCENT, MemoryGuard, available_memory, clean_spills,
                          format_bytes)
from parallel_backtest import max_workers, parse_workers, run_parallel
from scenario_editor import describe_mode as describe_data_mode, load_options
import synthetic
from performance_log import DEFAULT_PATH as PERFORMANCE_LOG, describe_update, update_performance_log
from model_config import CompiledModel, ProfileError, is_legacy_profile
from engine import QuantitativeTradingEngine, ensure_coinbase_sdk, load_environment_config

try:
    import tkinter as tk
    from tkinter import ttk, messagebox

    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure

    from chart import MODEL_COLORS, BacktestChart, ModelValueChart
except ImportError:  # pragma: no cover - GUI is optional in headless env
    tk = None
    ttk = None
    messagebox = None


class SimulationProfile:
    @staticmethod
    def from_json(path):
        """The profile file's raw JSON; describe_profile and run_backtest validate it."""
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


def _duration(candles, granularity_seconds):
    seconds = candles * granularity_seconds
    if seconds >= 86_400:
        return f"{seconds / 86_400:g} days"
    return f"{seconds / 3600:g} hours"


def format_regimes(regimes, granularity_seconds):
    """Report lines for a profile's regimes (a result's "regimes" or Regimes.summary-style dict): how they are
    detected and what each one trades."""
    lines = ["", f"Regimes ({len(regimes['summary']) - 1} plus base): a new regime takes over after it has held for "
                 f"{_duration(regimes['confirm_candles'], granularity_seconds)}, then stays at least "
                 f"{_duration(regimes['min_hold_candles'], granularity_seconds)}; on a switch an open position is "
                 f"{'kept' if regimes['on_switch'] == 'keep' else 'sold'}."]
    for regime in regimes["summary"]:
        settings = regime["settings"]
        stops = []
        if settings["use_hard_stop"] and settings["hard_stop_trigger"] and settings["hard_stop_percentage"]:
            stops.append(f"hard stop {settings['hard_stop_percentage']:.0%}")
        if settings["trailing_stop"]:
            stops.append(f"trailing stop {settings['trailing_stop']:.0%}")
        if settings["profit_lock_target"]:
            stops.append(f"profit target {settings['profit_lock_target']:.0%}")
        stops.append(f"position {settings['target_position_fraction']:.0%} of cash")
        when = f"when {regime['when']}" if regime["when"] else "when no other regime is active"
        lines.append(f"- {regime['name']} ({when}): {', '.join(stops)}")
        for label, all_key, any_key in (("buy", "entry_all", "entry_any"), ("sell", "exit_all", "exit_any")):
            rules = [f"all of: {rule}" for rule in regime[all_key]] + [f"any of: {rule}" for rule in regime[any_key]]
            lines.append(f"    {label}: " + ("; ".join(rules) if rules else "no rules"))
    return lines


def format_regime_activity(result, recent=10):
    """Report lines for the time a run spent in each regime and its latest switches."""
    regimes = result.get("regimes")
    if not regimes:
        return []
    share = ", ".join(f"{name} {fraction:.0%}" for name, fraction in regimes["share"].items())
    switches = regimes["switches"]
    lines = ["", f"Time in each regime: {share}", f"Regime switches: {len(switches):,}"]
    lines += [f"- {_format_time(switch['time'])}: {switch['from']} -> {switch['to']}" for switch in switches[-recent:]]
    return lines


def describe_profile(profile):
    """Validate a profile; return (is_valid, report text).

    Load Profile shows the report; Run Simulation refuses to start while any profile is invalid.
    """
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
    regimes = []
    if model.regimes is not None:
        regimes = format_regimes({"summary": model.regimes.summary(), "confirm_candles": model.regimes.confirm,
                                  "min_hold_candles": model.regimes.min_hold, "on_switch": model.regimes.on_switch},
                                 granularity_seconds)
    lines = (["Profile is valid.", "", f"Starting value: {starting_capital:,.2f} ({source})",
              f"Flash-crash buys: {flash}"]
             + format_model(model.indicator_meta(), model.rule_text()) + regimes + notes)
    return True, "\n".join(lines)


def format_fee_line(friction):
    """One-line summary of the fee rates a run is using."""
    line = (f"taker {friction['taker_rate']:.2%} / maker {friction['maker_rate']:.2%} "
            f"({friction['commission_source']})")
    if friction["network_fee_btc"]:
        line += f"; BTC network fee {friction['network_fee_btc']:.8f} BTC"
    return line


def format_friction(result):
    """Report lines for the fee, slippage and tax-reservation model."""
    friction = result["friction"]
    lines = [
        "",
        "Friction model:",
        f"- Exchange fees: {format_fee_line(friction)}",
        f"- Charged per fill: {friction['fill_fee_rate']:.3%} commission + {friction['slippage']:.2%} slippage",
        f"- Liquidity buffer (screen only): {friction['liquidity_fee_buffer']:.0%} of round-trip fees"
        f" + network fee ({friction['network_fee_detail']})",
        f"- Tax: {friction.get('tax_rule') or format(friction['tax_rate'], '.1%') + ' of each profitable trade'}",
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

    yearly = friction.get("tax_loss_offset", False)
    lines += ["", f"Taxes paid: {result['taxes_paid']:,.2f}",
              f"Tax held for the next {'yearly' if yearly else 'monthly'} payment: {result['taxes_reserved']:,.2f}"]
    for payment in result["tax_payments"]:
        carried, returned = payment.get("loss_carried_forward"), payment.get("refund")
        if payment["tax"] or carried or returned:
            period = str(payment["year"]) if yearly else f"{payment['year']}-{payment['month']:02d}"
            lines.append(
                f"- {period}: net realized {payment['net_realized_gain']:+,.2f}, tax paid {payment['tax']:,.2f}"
                + (f", {returned:,.2f} set aside beyond it returned" if returned else "")
                + (f", loss carried forward {carried:,.2f}" if carried else "")
            )
    return lines


def versus_buy_and_hold(result):
    """How much bigger (+) or smaller (-) the model's ending value is than buying and holding."""
    held = 1 + result["buy_and_hold_return"]
    return (1 + result["total_return"]) / held - 1 if held > 0 else float("nan")


def format_buy_and_hold(result):
    """Report lines for the buy-and-hold benchmark, where it starts, and the model's first buy."""
    start = result.get("buy_and_hold_start", result["period_start"])
    if start == result["period_start"]:
        where = f"from the test start, {_format_time(start)}"
    else:
        where = f"from {_format_time(start)}, the first candle the model could buy on"
    versus = versus_buy_and_hold(result)
    lines = [
        f"Buy and hold return: {result['buy_and_hold_return']:+.2%} ({where})",
        f"Model vs buy and hold: {versus:+.2%} "
        f"({'bigger' if versus >= 0 else 'smaller'} ending value than buying and holding)",
    ]
    if result.get("requested_start") is None:  # with a start date, format_test_start reports it
        first = result.get("first_entry")
        lines.append(f"First buy: {_format_time(first)}" if first is not None else "First buy: none; the model never bought")
    if start != result["period_start"]:
        lines.append(f"Buy and hold from the test start: {result['buy_and_hold_from_test_start']:+.2%}")
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
        *format_buy_and_hold(result),
        f"Max drawdown: {result['max_drawdown']:.2%} (tax reserves are removed from trading equity when withheld)",
        f"Worst drawdown within any {result['drawdown_window']}: {result['max_window_drawdown']:.2%}",
        f"Trades: {result['trade_count']:,} (win rate {result['win_rate']:.1%})",
        f"Fees paid: {result['fees_paid']:,.2f} at {result['commission_rate']:.3%} per fill",
        f"Position open at end: {'yes' if result['open_position'] else 'no'}",
    ]
    lines += format_model(result["indicators"], result["rules"])
    if result.get("regimes"):
        times = result["series"]["time"]
        step = times[1] - times[0] if len(times) > 1 else 86_400
        lines += format_regimes(result["regimes"], step)
        lines += format_regime_activity(result, recent_trades)
    lines += format_friction(result)
    events = result.get("flash_crash_events", [])
    if events:
        lines += ["", f"Flash-crash events: {len(events):,}"]
        for event in events:
            lasted = "never recovered" if event["minutes"] is None else f"recovered after {event['minutes']:,.0f} min"
            lines.append(f"- {_format_time(event['start_time'])}: from {event['reference']:,.2f} down to "
                         f"{event['low']:,.2f}, {lasted}; held through")
    flash = result["flash_crash_buys"]
    if flash:
        lines += ["", f"Flash-crash buys: {len(flash):,} fills, net {sum(t['pnl'] for t in flash):+,.2f}"]
        lines += [f"- {_format_time(t['entry_time'])}: {t['entry_reason']} bought at {t['entry_price']:,.2f}, "
                  f"sold at {t['exit_price']:,.2f} ({t['pnl']:+,.2f})" for t in flash[-10:]]
    if result.get("halted_at") is not None:
        lines.append(f"Loss halt: stopped trading for good at {_format_time(result['halted_at'])}")
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
    ("pnl", "Net P&L", 90, "e"),
    ("tax", "Tax reserved", 90, "e"),
    ("return", "Return", 75, "e"),
    ("outcome", "Outcome", 70, "w"),
    ("value_after", "Value after", 95, "e"),
    ("exit_reason", "Exit reason", 120, "w"),
]


def format_elapsed(seconds):
    """A run's duration to a tenth of a second, such as "8.4 s", "2 min 05.3 s" or "1 h 02 min"."""
    tenths = round(seconds * 10)
    hours, tenths = divmod(tenths, 36_000)
    minutes, tenths = divmod(tenths, 600)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {tenths / 10:04.1f} s"
    return f"{tenths / 10:.1f} s"


def format_worker_row(row):
    """(worker, what it is running, progress) for the Workers table, from a stress_test worker row."""
    if row["doing"] is None:
        return f"Worker {row['worker']}", "idle, waiting for the next task", ""
    filled = int(row["fraction"] * 10 + 1e-9)
    return (f"Worker {row['worker']}", row["doing"],
            "\u2588" * filled + "\u2591" * (10 - filled) + f" {row['fraction']:.0%}")


def parse_memory_percent(text):
    """The Memory limit box's percentage; raises ValueError for anything but a number above 0 and at most 100."""
    try:
        percent = float(str(text).strip().rstrip("%"))
    except ValueError:
        percent = None
    if percent is None or not 0 < percent <= 100:
        raise ValueError(f"The memory limit must be a percentage above 0 and at most 100, not {text!r}")
    return percent


def join_notes(*notes):
    return "; ".join(note for note in notes if note)


def describe_mode(workers, model_count):
    """How a run of `model_count` models on `workers` executes (see _simulation_worker)."""
    if workers > 1 and model_count > 1:
        return f"parallel, {min(workers, model_count)} workers"
    return "single-threaded" + (" (one model)" if workers > 1 else "")


def compare_timings(timings):
    """A line comparing the last single-threaded and parallel runs, or "" until both modes have run.

    `timings` maps "single" and "parallel" to {"seconds", "models" (names), "mode"}.
    """
    single, parallel = timings.get("single"), timings.get("parallel")
    if not single or not parallel:
        return ""
    line = (f"Last single-threaded run: {format_elapsed(single['seconds'])} for {len(single['models'])} models; "
            f"last parallel run ({parallel['mode'].removeprefix('parallel, ')}): "
            f"{format_elapsed(parallel['seconds'])} for {len(parallel['models'])} models")
    if sorted(single["models"]) != sorted(parallel["models"]):
        return line + " (different models: run both modes on the same selection to compare)"
    ratio = single["seconds"] / max(parallel["seconds"], 1e-9)
    if ratio >= 1:
        return line + f". Parallel was {ratio:.1f}x as fast, saving {format_elapsed(single['seconds'] - parallel['seconds'])}"
    return line + f". Single-threaded was {1 / ratio:.1f}x as fast"


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
        net_pnl = trade.get("net_pnl", trade["pnl"])
        tax_withheld = trade.get("tax_withheld", 0.0)
        outcome = "Win" if net_pnl > 0 else "Loss"
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
                "pnl": f"{net_pnl:+,.2f}",
                "tax": f"{tax_withheld:,.2f}",
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
                "pnl": net_pnl,
                "tax": tax_withheld,
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
    wins = sum(1 for trade in trades if trade.get("net_pnl", trade["pnl"]) > 0)
    returns = [trade["return"] for trade in trades]
    net_pnl = sum(trade.get("net_pnl", trade["pnl"]) for trade in trades)
    exits = {}
    for trade in trades:
        exits[trade["exit_reason"]] = exits.get(trade["exit_reason"], 0) + 1
    return (
        f"{len(trades):,} trades: {wins:,} wins, {len(trades) - wins:,} losses "
        f"({wins / len(trades):.1%} win rate). Net P&L {net_pnl:+,.2f}. "
        f"Average return {sum(returns) / len(returns):+.2%}, best {max(returns):+.2%}, "
        f"worst {min(returns):+.2%}.\n"
        "Exits: " + ", ".join(f"{reason} {count:,}" for reason, count in
                              sorted(exits.items(), key=lambda item: -item[1]))
    )


CONFIG_DIRECTORY = Path(__file__).parent / "config"
ALL_CURRENCIES = "All currencies"
# The Models list's Type filter: every model, only models with market regimes (see regimes.py), or only those without.
ALL_MODELS, REGIME_MODELS, STANDARD_MODELS = "All models", "Regime models", "Models without regimes"
MODEL_TYPES = [ALL_MODELS, REGIME_MODELS, STANDARD_MODELS]


def model_matches(product, has_regimes, currency=ALL_CURRENCIES, model_type=ALL_MODELS):
    """Whether a model of `product` (with or without regimes) passes the Models list's Currency and Type filters."""
    if currency != ALL_CURRENCIES and product != currency:
        return False
    return model_type == ALL_MODELS or has_regimes == (model_type == REGIME_MODELS)

# (key, heading, width in pixels, anchor) for each Comparison table column.
COMPARISON_COLUMNS = [
    ("model", "Model", 200, "w"),
    ("data", "Data", 190, "w"),
    ("product", "Product", 80, "w"),
    ("candles", "Candles", 130, "w"),
    ("start", "Start", 90, "e"),
    ("end", "End", 110, "e"),
    ("return", "Return", 90, "e"),
    ("first_buy", "First buy", 120, "w"),
    ("bh_start", "B&H start", 120, "w"),
    ("buy_and_hold", "Buy & hold", 90, "e"),
    ("versus", "vs Buy & hold", 105, "e"),
    ("max_drawdown", "Max drawdown", 110, "e"),
    ("window_drawdown", "Worst in window", 125, "e"),
    ("flash", "Flash buys", 80, "e"),
    ("trades", "Trades", 60, "e"),
    ("win_rate", "Win rate", 70, "e"),
    ("fees", "Fees", 90, "e"),
    ("taxes", "Taxes", 90, "e"),
    ("blocked", "Blocked signals", 105, "e"),
]


# Comparison columns whose sort value is a fraction shown as a percentage: filters compare in percent.
PERCENT_COLUMNS = {"return", "buy_and_hold", "versus", "max_drawdown", "window_drawdown", "win_rate"}
# Columns filtered by matching their text (dates and names); the rest compare numbers.
TEXT_COLUMNS = {"model", "data", "product", "candles", "first_buy", "bh_start"}
# Number filters a column's drop-down offers, as in Excel: name -> test(value, first, second).
NUMBER_TESTS = {
    "greater than": lambda value, a, b: value > a,
    "at least": lambda value, a, b: value >= a,
    "less than": lambda value, a, b: value < a,
    "at most": lambda value, a, b: value <= a,
    "equals": lambda value, a, b: value == a,
    "between": lambda value, a, b: min(a, b) <= value <= max(a, b),
}


def _number(text):
    return float(str(text).replace("%", "").replace("$", "").replace(",", "").replace("x", "").strip())


def filter_number(row, key):
    """A row's value in a number column, in the units shown (percent columns in percent); None if it has none."""
    value = row["sort"][key]
    if not isinstance(value, (int, float)) or key in TEXT_COLUMNS or math.isinf(value):
        return None
    return value * 100 if key in PERCENT_COLUMNS else value


def column_values(rows, key):
    """A column's distinct values as shown, in sort order (numbers smallest first, text A to Z)."""
    first = {}
    for row in rows:
        first.setdefault(row["display"][key], row["sort"][key])
    def order(item):
        shown, value = item
        return (0, value, shown) if isinstance(value, (int, float)) and not isinstance(value, bool) else (1, 0, str(shown).lower())
    return [shown for shown, _ in sorted(first.items(), key=order)]


def row_passes(row, filters):
    """Whether a comparison row passes every column filter.

    `filters` maps a column to {"values": the shown values to keep, or None for all, "test": None or
    (number test name, first, second)}, as the Excel-style drop-downs set them.
    """
    for key, column_filter in filters.items():
        values = column_filter.get("values")
        if values is not None and row["display"][key] not in values:
            return False
        test = column_filter.get("test")
        if test:
            number = filter_number(row, key)
            if number is None or not NUMBER_TESTS[test[0]](number, test[1], test[2]):
                return False
    return True


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


def default_view(results, limit=None):
    """Positions of the models charted after a run: all of them, or the best `limit` by return.

    The limit defaults to one model per distinct line color, so a run of many models opens
    on a readable chart.
    """
    limit = limit or len(MODEL_COLORS)
    best = sorted(range(len(results)), key=lambda position: results[position]["total_return"], reverse=True)
    return sorted(best[:limit])


def assign_colors(slots, positions):
    """Each model's color slot: a model keeps its slot while it stays in view; one joining takes the lowest free one."""
    kept = {position: slot for position, slot in slots.items() if position in positions}
    taken = set(kept.values())
    for position in positions:
        if position not in kept:
            slot = next(slot for slot in range(len(positions) + len(taken) + 1) if slot not in taken)
            kept[position] = slot
            taken.add(slot)
    return kept


def matching_models(names, text):
    """Positions of the names containing every word of `text`, ignoring case; all of them when blank."""
    words = str(text).lower().split()
    return [position for position, name in enumerate(names) if all(word in name.lower() for word in words)]


def parse_floor(text):
    """The chart floor as a positive number, or None when blank.

    Accepts thousands separators and a leading dollar sign, such as "1,000" or "$500".
    """
    cleaned = (text or "").strip().lstrip("$").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"Floor {text.strip()!r} must be a number, such as 1000") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Floor must be a number above zero, because the charts use a log scale")
    return value


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


HISTORIC = "Historic"


def is_historic(result):
    """A real-history result that counts for the performance log and best models (a stress test's don't)."""
    return result.get("data_source", HISTORIC) == HISTORIC and not result.get("stress_test")


def top_by_history(paths, products, rows, count):
    """The `count` models of each currency with the highest logged full-history growth, as a set of paths.

    `products` maps each path to its product; `rows` are performance-log rows by model name. A
    row logged for another version of the profile doesn't count.
    """
    from performance_log import profile_fingerprint
    by_product = {}
    for path in paths:
        row = rows.get(Path(path).stem)
        profile = read_profile(path)
        if row is None or profile is None or row.get("profile_fingerprint") not in ("", None,
                                                                                    profile_fingerprint(profile)):
            continue
        try:
            growth = float(row["growth_multiple"])
        except (KeyError, TypeError, ValueError):
            continue
        by_product.setdefault(products.get(path, ""), []).append((growth, path))
    return {path for ranked in by_product.values() for _, path in sorted(ranked, reverse=True)[:count]}


def scenario_matrix(results):
    """Rows for the Scenarios tab: each model's historic return, its return in each scenario (the median
    and worst over a scenario's paths), its worst result, and in how many scenarios it beat buy and hold."""
    scenarios, by_model = [], {}
    for result in results:
        source = result.get("data_source", HISTORIC)
        if source != HISTORIC and source not in scenarios:
            scenarios.append(source)
        model = result.get("base_model", result.get("model_name", ""))
        by_model.setdefault(model, {"product": result["product_id"]}).setdefault(source, []).append(result)
    rows = []
    for model, sources in by_model.items():
        cells, worst, beat, tested = {}, None, 0, 0
        for source in [HISTORIC] + scenarios:
            runs = sources.get(source)
            if not runs:
                cells[source] = None
                continue
            returns = sorted(run["total_return"] for run in runs)
            cells[source] = (returns[len(returns) // 2], returns[0], len(returns))
            if source != HISTORIC:
                worst = returns[0] if worst is None else min(worst, returns[0])
                tested += len(runs)
                beat += sum(run["total_return"] > run["buy_and_hold_return"] for run in runs)
        rows.append({"model": model, "product": sources["product"], "cells": cells, "worst": worst,
                     "beat": beat, "tested": tested})
    rows.sort(key=lambda row: (row["worst"] if row["worst"] is not None else float("-inf")), reverse=True)
    return [HISTORIC] + scenarios, rows


def scenario_groups(results):
    """The data sets a run tested on, each with the results run on it, for the Scenario charts tab.

    A data set is one source (the real history, a scenario, or a stress combination and mode) on one
    path, for one market. Returns [{"label", "source", "path", "product", "granularity",
    "positions" (in `results`), "start" (where the scenario's candles begin, or None)}],
    history first, then in the order the results came.
    """
    groups, paths = {}, {}
    for position, result in enumerate(results):
        source = result.get("data_source", HISTORIC)
        path = result.get("scenario_path", 0)
        market = (result["product_id"], result.get("granularity", ""))
        groups.setdefault((source, path, *market), []).append(position)
        paths.setdefault((source, *market), set()).add(path)
    rows = []
    for (source, path, product, granularity), positions in groups.items():
        several = len(paths[(source, product, granularity)]) > 1
        name = source + (f" #{path + 1}" if several else "")
        rows.append({"label": f"{name} \u00b7 {product} {granularity}".rstrip(), "source": source, "path": path,
                     "paths": len(paths[(source, product, granularity)]), "market": f"{product} {granularity}".rstrip(),
                     "product": product, "granularity": granularity, "positions": positions,
                     "start": results[positions[0]].get("scenario_start")})
    rows.sort(key=lambda row: row["source"] != HISTORIC)  # stable: history first, then as they came
    return rows


def best_by_currency(results):
    """{product: position in `results`} of each currency's best result by total return."""
    best = {}
    for position, result in enumerate(results):
        product = result["product_id"]
        if product not in best or result["total_return"] > results[best[product]]["total_return"]:
            best[product] = position
    return best


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
            "data": result.get("data_source", HISTORIC),
            "product": result["product_id"],
            "candles": result.get("granularity", ""),
            "start": result["starting_capital"],
            "end": result["ending_equity"],
            "return": result["total_return"],
            "first_buy": result.get("first_entry"),
            "bh_start": result.get("buy_and_hold_start", result.get("period_start")),
            "buy_and_hold": result["buy_and_hold_return"],
            "versus": versus_buy_and_hold(result),
            "max_drawdown": result["max_drawdown"],
            "window_drawdown": result["max_window_drawdown"],
            "flash": len(result["flash_crash_buys"]),
            "trades": result["trade_count"],
            "win_rate": result["win_rate"],
            "fees": result["fees_paid"],
            "taxes": result["taxes_paid"] + result.get("taxes_reserved", 0.0),
            "blocked": result["screened_signals"],
        }
        display = dict(values)
        display["first_buy"] = _short_time(values["first_buy"]) if values["first_buy"] is not None else "never"
        if values["first_buy"] is None:
            values["first_buy"] = float("inf")  # models that never bought sort after every date
        display["bh_start"] = _short_time(values["bh_start"]) if values["bh_start"] is not None else "test start"
        if values["bh_start"] is None:
            values["bh_start"] = 0
        for key in ("start", "end", "fees", "taxes"):
            display[key] = f"{values[key]:,.2f}"
        for key in ("return", "buy_and_hold"):
            display[key] = f"{values[key]:+.2%}"
        display["versus"] = f"{values['versus']:+.1%}"
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


class ColumnFilterPopup:
    """An Excel-style drop-down for one comparison column: sort, a number filter, and a checklist of values.

    The checklist offers the values left by the other columns' filters. Searching narrows it;
    with a search, OK keeps only the checked values the search shows, as Excel does.
    """

    CHECKED, UNCHECKED = "\u2611", "\u2610"

    def __init__(self, window, key):
        self.window, self.key = window, key
        self.heading = dict((k, h) for k, h, *_ in COMPARISON_COLUMNS)[key]
        others = {k: f for k, f in window.column_filters.items() if k != key}
        rows = [row for row in window.comparison_rows.values() if row_passes(row, others)]
        self.values = column_values(rows, key)
        current = window.column_filters.get(key, {})
        self.checked = set(current.get("values") or self.values)
        self.numeric = key not in TEXT_COLUMNS

        top = self.top = tk.Toplevel(window.root)
        top.title(f"Filter: {self.heading}")
        top.transient(window.root)
        x, y = window.root.winfo_pointerx(), window.root.winfo_pointery()
        top.geometry(f"320x480+{max(x - 40, 0)}+{y + 12}")
        frame = ttk.Frame(top, padding=8)
        frame.pack(fill="both", expand=True)
        ascending, descending = ("Sort A to Z", "Sort Z to A") if not self.numeric else (
            "Sort smallest to largest", "Sort largest to smallest")
        ttk.Button(frame, text=ascending, command=lambda: self.sort(False)).pack(fill="x")
        ttk.Button(frame, text=descending, command=lambda: self.sort(True)).pack(fill="x", pady=(2, 0))
        ttk.Button(frame, text=f'Clear filter from "{self.heading}"', command=self.clear).pack(fill="x", pady=(2, 6))
        self.test_var, self.first_var, self.second_var = tk.StringVar(value="(none)"), tk.StringVar(), tk.StringVar()
        if self.numeric:
            box = ttk.LabelFrame(frame, text="Number filter" + (" (in %)" if key in PERCENT_COLUMNS else ""),
                                 padding=4)
            box.pack(fill="x", pady=(0, 6))
            ttk.Combobox(box, textvariable=self.test_var, state="readonly", width=12,
                         values=["(none)", *NUMBER_TESTS]).pack(side="left")
            ttk.Entry(box, textvariable=self.first_var, width=9).pack(side="left", padx=(4, 0))
            ttk.Label(box, text="and").pack(side="left", padx=2)
            ttk.Entry(box, textvariable=self.second_var, width=9).pack(side="left")
            if current.get("test"):
                name, first, second = current["test"]
                self.test_var.set(name)
                self.first_var.set(f"{first:g}")
                self.second_var.set("" if second is None else f"{second:g}")
        self.search_var = tk.StringVar()
        search_row = ttk.Frame(frame)
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Search:").pack(side="left")
        ttk.Entry(search_row, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._show())
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.tree = ttk.Treeview(list_frame, show="tree", selectmode="none")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<ButtonRelease-1>", self._clicked)
        self.note_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.note_var, foreground="#b3261e").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(buttons, text="OK", command=self.apply).pack(side="right", padx=(0, 4))
        top.bind("<Escape>", lambda _event: self.close())
        top.bind("<Return>", lambda _event: self.apply())
        self._show()

    def visible(self):
        words = self.search_var.get().lower().split()
        return [value for value in self.values if all(word in str(value).lower() for word in words)]

    def _show(self):
        self.tree.delete(*self.tree.get_children())
        shown = self.visible()
        everything = bool(shown) and all(value in self.checked for value in shown)
        self.tree.insert("", "end", iid="__all__", text=f"{self.CHECKED if everything else self.UNCHECKED} (Select All)")
        for index, value in enumerate(shown):
            mark = self.CHECKED if value in self.checked else self.UNCHECKED
            self.tree.insert("", "end", iid=f"v{index}", text=f"{mark} {value}")
        self._shown = shown

    def _clicked(self, event):
        item = self.tree.identify_row(event.y)
        if item == "__all__":
            self.select_all(not all(value in self.checked for value in self._shown))
        elif item:
            self.toggle(self._shown[int(item[1:])])

    def toggle(self, value):
        self.checked ^= {value}
        self._show()

    def select_all(self, on):
        """Check (or uncheck) every value the search shows."""
        (self.checked.update if on else self.checked.difference_update)(self.visible())
        self._show()

    def sort(self, descending):
        self.window._sort_comparison(self.key, descending)
        self.close()

    def clear(self):
        self.window.set_column_filter(self.key)
        self.close()

    def apply(self):
        """Apply the checked values and the number filter, then close; an empty choice is refused."""
        keep = self.checked & set(self.visible()) if self.search_var.get().strip() else set(self.checked)
        if not keep:
            self.note_var.set("Check at least one value")
            return
        test = None
        if self.test_var.get() != "(none)":
            try:
                first = _number(self.first_var.get())
                second = _number(self.second_var.get()) if self.test_var.get() == "between" else None
            except ValueError:
                self.note_var.set("The number filter needs a number" + (
                    "s in both boxes" if self.test_var.get() == "between" else ""))
                return
            test = (self.test_var.get(), first, second)
        all_values = keep >= set(self.values)
        self.window.set_column_filter(self.key, None if all_values else keep, test)
        self.close()

    def close(self):
        if self.top.winfo_exists():
            self.top.destroy()
        if self.window.filter_popup is self:
            self.window.filter_popup = None


class SimulationWindow:
    """Pick models, run them on a worker thread, and browse the results in tabs."""

    def __init__(self, root):
        self.root = root
        self.root.title("Quant Bot Simulator")
        self.root.geometry("1200x900")
        self.root.minsize(900, 700)

        self.model_paths = []
        self.model_products = {}  # path -> product_id
        self.regime_paths = set()  # listed models with market regimes (the Type filter)
        self.selected_paths = set()  # chosen models, kept while the Currency or Type filter hides them
        self.shown_paths = []  # the models the Available list shows, in its order
        self.selection_order = []  # the Selected list's order (selected_paths says which are selected)
        self.best_paths = set()  # each currency's recorded best model
        self.discarded_paths = set()  # listed models from discard folders (with "Include discarded models")
        self.top_paths = set()  # each currency's historically best models (Test options: top N)
        self.test_options = load_options()
        # The memory free at launch: each run may use the Memory limit's share of it (see memory_guard.py).
        self.launch_free_memory = available_memory()
        self.memory_guard = None  # each run makes its own (see _run_guard)
        clean_spills()  # chart data earlier sessions kept on disk; their results are gone
        self.results = []
        self.in_view = []  # positions in self.results of the models the charts show
        self.color_slots = {}  # position -> color slot, stable while the model stays in view
        self._view_job = None  # a pending view change from the Model value list
        self.current = None  # the position the Chart, Trades and Metrics tabs show
        self.result_var = tk.StringVar(value="Simulation status: idle")
        self.auth_status_var = tk.StringVar(value="Coinbase authentication: not tested")
        self.runtime_config = load_environment_config()
        self.auth_engine = QuantitativeTradingEngine(
            base_directory=".", node_is_primary=False, env_file=".env"
        )
        # The worker thread reports through this queue; only the Tk thread touches widgets.
        self.progress_queue = queue.Queue()
        self.simulation_thread = None
        self.full_screen = False
        self.performance_log_path = PERFORMANCE_LOG
        self.floor_notes = []  # (chart, note variable) for each chart tab's floor control

        self._build_ui()
        self.root.bind("<F11>", lambda _event: self._set_full_screen(not self.full_screen))
        self.root.bind("<Escape>", lambda _event: self._set_full_screen(False))

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # Everything above the results; full screen hides it so the charts get the room.
        self.controls = ttk.Frame(main)
        self.controls.pack(fill="x")
        heading_row = ttk.Frame(self.controls)
        heading_row.pack(fill="x")
        ttk.Label(heading_row, text="Models (move them to Selected to test them; \u2605 each currency's best, "
                                    "\u25b2 its top models)",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        self.currency_var = tk.StringVar(value=ALL_CURRENCIES)
        self.currency_picker = ttk.Combobox(heading_row, textvariable=self.currency_var, state="readonly", width=16,
                                            values=[ALL_CURRENCIES])
        self.currency_picker.pack(side="right")
        self.currency_picker.bind("<<ComboboxSelected>>", lambda _event: self._fill_model_list())
        ttk.Label(heading_row, text="Currency:").pack(side="right", padx=(0, 6))
        self.model_type_var = tk.StringVar(value=ALL_MODELS)
        self.model_type_picker = ttk.Combobox(heading_row, textvariable=self.model_type_var, state="readonly",
                                              width=22, values=MODEL_TYPES)
        self.model_type_picker.pack(side="right", padx=(0, 16))
        self.model_type_picker.bind("<<ComboboxSelected>>", lambda _event: self._fill_model_list())
        ttk.Label(heading_row, text="Type:").pack(side="right", padx=(0, 6))
        self.include_discarded_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(heading_row, text="Include discarded models", variable=self.include_discarded_var,
                        command=self._refresh_models).pack(side="right", padx=(0, 16))
        # Two lists, as in a "slush bucket": models move from Available to Selected rather than being highlighted.
        models_row = ttk.Frame(self.controls)
        models_row.pack(fill="x", pady=(6, 12))
        models_row.columnconfigure(0, weight=1)
        models_row.columnconfigure(2, weight=1)
        available_frame = ttk.Frame(models_row)
        available_frame.grid(row=0, column=0, sticky="nsew")
        available_top = ttk.Frame(available_frame)
        available_top.pack(fill="x")
        self.available_count_var = tk.StringVar(value="Available")
        ttk.Label(available_top, textvariable=self.available_count_var).pack(side="left")
        self.model_search_var = tk.StringVar()
        self.model_search_var.trace_add("write", lambda *_: self._fill_model_list())
        ttk.Entry(available_top, textvariable=self.model_search_var, width=22).pack(side="right")
        ttk.Label(available_top, text="Search:").pack(side="right", padx=(0, 4))
        available_body = ttk.Frame(available_frame)
        available_body.pack(fill="both", expand=True, pady=(2, 0))
        self.model_list = tk.Listbox(available_body, selectmode="extended", height=7, exportselection=False,
                                     activestyle="none")
        available_scroll = ttk.Scrollbar(available_body, orient="vertical", command=self.model_list.yview)
        self.model_list.configure(yscrollcommand=available_scroll.set)
        self.model_list.pack(side="left", fill="both", expand=True)
        available_scroll.pack(side="left", fill="y")
        self.model_list.bind("<Double-1>", lambda _event: self._add_models())
        self.model_list.bind("<Return>", lambda _event: self._add_models())

        move_buttons = ttk.Frame(models_row)
        move_buttons.grid(row=0, column=1, padx=8, sticky="n", pady=(18, 0))
        for text, command in (("Add \u203a", self._add_models), ("Add all \u203a\u203a", self._add_all_models),
                              ("\u2039 Remove", self._remove_models),
                              ("\u2039\u2039 Remove all", self._remove_all_models),
                              ("Move up", lambda: self._move_selected(-1)),
                              ("Move down", lambda: self._move_selected(1))):
            ttk.Button(move_buttons, text=text, width=13, command=command).pack(fill="x", pady=(0, 3))

        selected_frame = ttk.Frame(models_row)
        selected_frame.grid(row=0, column=2, sticky="nsew")
        self.selected_count_var = tk.StringVar(value="Selected")
        ttk.Label(selected_frame, textvariable=self.selected_count_var).pack(anchor="w", pady=(0, 3))
        selected_body = ttk.Frame(selected_frame)
        selected_body.pack(fill="both", expand=True)
        self.selected_list = tk.Listbox(selected_body, selectmode="extended", height=7, exportselection=False,
                                        activestyle="none")
        selected_scroll = ttk.Scrollbar(selected_body, orient="vertical", command=self.selected_list.yview)
        self.selected_list.configure(yscrollcommand=selected_scroll.set)
        self.selected_list.pack(side="left", fill="both", expand=True)
        selected_scroll.pack(side="left", fill="y")
        self.selected_list.bind("<Double-1>", lambda _event: self._remove_models())
        self.selected_list.bind("<Delete>", lambda _event: self._remove_models())

        model_buttons = ttk.Frame(models_row)
        model_buttons.grid(row=0, column=3, padx=(8, 0), sticky="n", pady=(18, 0))
        ttk.Button(model_buttons, text="Add file...", command=self._browse_config).pack(fill="x")
        ttk.Button(model_buttons, text="Refresh", command=self._refresh_models).pack(fill="x", pady=(4, 0))
        self.select_best_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.controls, text="After a run, select the best model of each currency tested",
                        variable=self.select_best_var).pack(anchor="w", pady=(0, 8))
        self._refresh_models()

        start_row = ttk.Frame(self.controls)
        start_row.pack(fill="x", pady=(0, 10))
        ttk.Label(start_row, text="Start testing from (UTC, optional):").pack(side="left")
        self.start_date_var = tk.StringVar(value="")
        ttk.Entry(start_row, textvariable=self.start_date_var, width=18).pack(side="left", padx=(6, 0))
        self.fresh_signal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(start_row, text="Wait for a fresh entry signal", variable=self.fresh_signal_var).pack(
            side="left", padx=(12, 0))
        ttk.Label(start_row, text="e.g. 2020-01-01. Blank tests all 10 years. The earlier history still warms up "
                                  "the indicators.", foreground="#555555").pack(side="left", padx=(12, 0))

        options_row = ttk.Frame(self.controls)
        options_row.pack(fill="x", pady=(0, 8))
        ttk.Button(options_row, text="Test options...", command=self._open_test_options).pack(side="left")
        self.test_mode_var = tk.StringVar(value=describe_data_mode(self.test_options))
        ttk.Label(options_row, textvariable=self.test_mode_var).pack(side="left", padx=(8, 0))
        ttk.Button(options_row, text="Select top models", command=self._select_top_models).pack(side="right")
        ttk.Label(options_row, text="\u25b2 marks each currency's historically best models",
                  foreground="#555555").pack(side="right", padx=(0, 8))
        action_row = ttk.Frame(self.controls)
        action_row.pack(fill="x", pady=(0, 12))
        ttk.Button(action_row, text="Load Profile", command=self._load_profile).pack(side="left")
        self.run_button = ttk.Button(action_row, text="Run Simulation", command=self._run_simulation)
        self.run_button.pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Test Coinbase Authentication", command=self._authenticate_coinbase).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(action_row, text="Clear data...", command=self._open_clear_data).pack(side="left", padx=(8, 0))
        self.parallel_var = tk.BooleanVar(value=True)
        self.mode_text = tk.StringVar()
        self.mode_button = ttk.Checkbutton(action_row, textvariable=self.mode_text, variable=self.parallel_var,
                                           style="Toolbutton", command=self._update_mode, width=31)
        self.mode_button.pack(side="left", padx=(16, 0))
        self.workers_label = ttk.Label(action_row, text="Workers:")
        self.workers_label.pack(side="left", padx=(8, 0))
        self.workers_var = tk.StringVar(value="auto")
        self.workers_box = ttk.Spinbox(action_row, values=["auto", *range(1, max_workers() + 1)],
                                       textvariable=self.workers_var, width=5)
        self.workers_box.pack(side="left", padx=(6, 0))
        ttk.Label(action_row, text="Memory limit:").pack(side="left", padx=(16, 0))
        self.memory_var = tk.StringVar(value=f"{DEFAULT_MEMORY_PERCENT:g}")
        ttk.Spinbox(action_row, from_=10, to=100, increment=5, textvariable=self.memory_var, width=5).pack(
            side="left", padx=(6, 0))
        free = (f"% of the {format_bytes(self.launch_free_memory)} free at launch"
                if self.launch_free_memory else "% (free memory can't be read here, so there is no limit)")
        ttk.Label(action_row, text=free, foreground="#555555").pack(side="left", padx=(4, 0))
        ttk.Label(action_row, text=f"auto uses {max_workers()} workers, each running one model on one set of "
                                   f"data at a time; scenario and stress runs add one per GPU",
                  foreground="#555555").pack(side="left", padx=(16, 0))
        self._update_mode()

        ttk.Label(self.controls, text="Progress", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        progress_frame = ttk.Frame(self.controls)
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
        ttk.Label(progress_frame, text="Time spent", width=24).grid(row=6, column=0, sticky="w")
        self.time_var = tk.StringVar(value="not run yet")
        ttk.Label(progress_frame, textvariable=self.time_var).grid(row=6, column=1, sticky="w")
        self.timing_compare_var = tk.StringVar(value="Run once in each mode, on the same models, to compare them")
        ttk.Label(progress_frame, textvariable=self.timing_compare_var, foreground="#555555").grid(
            row=7, column=1, sticky="w")
        # One row per worker process while a stress test runs (see _show_workers); hidden otherwise.
        self.workers_label = ttk.Label(progress_frame, text="Workers", width=24)
        self.workers_label.grid(row=8, column=0, sticky="nw", pady=(6, 0))
        self.workers_table = ttk.Treeview(progress_frame, columns=("worker", "doing", "progress"),
                                          show="headings", height=4, selectmode="none")
        for column, heading, width, stretch in (("worker", "Worker", 90, False), ("doing", "Running", 500, True),
                                                ("progress", "Progress", 150, False)):
            self.workers_table.heading(column, text=heading, anchor="w")
            self.workers_table.column(column, width=width, stretch=stretch, anchor="w")
        self.workers_table.grid(row=8, column=1, sticky="ew", pady=(6, 0))
        self._show_workers([])
        self.timings = {}  # "single" / "parallel" -> the last run in that mode, for compare_timings
        self.run_timing = None  # the run under way: started, mode, models

        # Status is packed from the bottom first so the expanding tabs never push it off-screen.
        ttk.Label(main, textvariable=self.auth_status_var, foreground="#1a5d1a").pack(
            side="bottom", anchor="w", pady=(4, 0)
        )
        ttk.Label(main, textvariable=self.result_var, foreground="#1a5d1a").pack(
            side="bottom", anchor="w", pady=(4, 0)
        )
        ttk.Label(main, text="Status", font=("Segoe UI", 10, "bold")).pack(side="bottom", anchor="w")

        picker_row = self.picker_row = ttk.Frame(main)
        picker_row.pack(fill="x", pady=(0, 6))
        self.full_screen_button = ttk.Button(picker_row, text="Full screen (F11)",
                                             command=lambda: self._set_full_screen(not self.full_screen))
        self.full_screen_button.pack(side="right")
        ttk.Label(picker_row, text="Showing model:").pack(side="left")
        self.selected_model_var = tk.StringVar(value="")
        self.model_picker = ttk.Combobox(picker_row, textvariable=self.selected_model_var, state="readonly", width=40)
        self.model_picker.pack(side="left", padx=(6, 0))
        self.model_picker.bind("<<ComboboxSelected>>",
                               lambda _event: self._show_model(self.in_view[self.model_picker.current()]))
        ttk.Button(picker_row, text="Choose models...", command=self._choose_models).pack(side="left", padx=(12, 0))
        ttk.Button(picker_row, text="Show all", command=lambda: self._set_view(range(len(self.results)))).pack(
            side="left", padx=(4, 0))
        self.view_var = tk.StringVar(value="")
        ttk.Label(picker_row, textvariable=self.view_var, foreground="#555555").pack(side="left", padx=(8, 0))

        self.results_tabs = ttk.Notebook(main)
        self.results_tabs.pack(fill="both", expand=True, pady=(0, 10))

        self.comparison_tab = self._add_comparison_tab()
        self.chart_tab, self.chart = self._add_chart_tab("Chart", BacktestChart, "Lowest price shown:")
        self.value_tab, self.value_chart = self._add_chart_tab("Model value", ModelValueChart, "Lowest value shown:",
                                                               model_list=True)
        self.trades_tab = self._add_trades_tab()
        self.scenarios_tab = self._add_scenarios_tab()
        self.scenario_charts_tab = self._add_scenario_charts_tab()

        self.metrics_tab = ttk.Frame(self.results_tabs)
        self.results_tabs.add(self.metrics_tab, text="Metrics")
        self.metrics_text = tk.Text(self.metrics_tab, wrap="word", bg="#f5f5f5")
        self.metrics_text.pack(fill="both", expand=True)

    def _update_mode(self):
        """Show the mode on the toggle button; the worker count only matters in parallel mode."""
        parallel = self.parallel_var.get()
        self.mode_text.set("Mode: parallel (multi-process)" if parallel else "Mode: single-threaded")
        for widget in (self.workers_label, self.workers_box):
            widget.state(["!disabled"] if parallel else ["disabled"])

    def _add_chart_tab(self, title, chart_class, floor_label, model_list=False):
        tab = ttk.Frame(self.results_tabs)
        self.results_tabs.add(tab, text=title)
        figure = Figure(figsize=(9, 5), dpi=100)
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab, pack_toolbar=False)
        chart = chart_class(figure, canvas)

        floor_row = ttk.Frame(tab)
        floor_row.pack(side="top", fill="x", pady=(6, 4))
        ttk.Label(floor_row, text=floor_label).pack(side="left")
        floor_var = tk.StringVar(value="")
        note_var = tk.StringVar()
        entry = ttk.Entry(floor_row, textvariable=floor_var, width=14)
        entry.pack(side="left", padx=(6, 0))
        apply = lambda *_: self._apply_floor(chart, floor_var, note_var)
        entry.bind("<Return>", apply)
        ttk.Button(floor_row, text="Apply", command=apply).pack(side="left", padx=(6, 0))
        ttk.Button(floor_row, text="Clear", command=lambda: (floor_var.set(""), apply())).pack(
            side="left", padx=(4, 0))
        ttk.Label(floor_row, textvariable=note_var, foreground="#555555").pack(side="left", padx=(12, 0))
        self.floor_notes.append((chart, note_var))
        self._describe_floor(chart, note_var)

        if model_list:
            self._add_view_list(tab)
        toolbar.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return tab, chart

    def _add_view_list(self, tab):
        """A list beside the Model value chart for picking the models it draws, best return first."""
        panel = ttk.Frame(tab, padding=(0, 0, 8, 0))
        panel.pack(side="left", fill="y")
        ttk.Label(panel, text="Models on the chart", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.view_search_var = tk.StringVar()
        search = ttk.Entry(panel, textvariable=self.view_search_var, width=30)
        search.pack(fill="x", pady=(4, 0))
        ttk.Label(panel, text="Search; click, Ctrl-click or Shift-click", foreground="#555555").pack(anchor="w")
        list_frame = ttk.Frame(panel)
        list_frame.pack(fill="both", expand=True, pady=(4, 0))
        # A light highlight, so each charted model's name keeps its line color (a key to the chart).
        self.view_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False, activestyle="none",
                                    width=32, selectbackground="#dbe7f5")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.view_list.yview)
        self.view_list.configure(yscrollcommand=scroll.set)
        self.view_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text=f"Best {len(MODEL_COLORS)}", width=7,
                   command=lambda: self._set_view(default_view(self.results))).pack(side="left")
        ttk.Button(buttons, text="All listed", width=9, command=lambda: self._view_listed(True)).pack(
            side="left", padx=(4, 0))
        ttk.Button(buttons, text="Drop listed", width=10, command=lambda: self._view_listed(False)).pack(
            side="left", padx=(4, 0))
        self.view_list_note = tk.StringVar(value="Run a simulation to list its models")
        ttk.Label(panel, textvariable=self.view_list_note, foreground="#555555", wraplength=230).pack(
            anchor="w", pady=(4, 0))
        self.view_listed = []  # positions in self.results, in list order
        self.view_search_var.trace_add("write", lambda *_: self._refresh_view_list())
        self.view_list.bind("<<ListboxSelect>>", lambda _event: self._view_list_picked())

    def _refresh_view_list(self):
        """List the models matching the search, best return first, with those in view selected."""
        names = [result["model_name"] for result in self.results]
        self.view_listed = sorted(matching_models(names, self.view_search_var.get()),
                                  key=lambda position: self.results[position]["total_return"], reverse=True)
        self.view_list.delete(0, "end")
        for row, position in enumerate(self.view_listed):
            self.view_list.insert("end", f"{names[position]}   {self.results[position]['total_return']:+.1%}")
            if position in self.in_view:
                self.view_list.select_set(row)
                self.view_list.itemconfigure(row, selectforeground=MODEL_COLORS[
                    self.color_slots.get(position, 0) % len(MODEL_COLORS)])
        self.view_list_note.set(f"{len(self.in_view)} of {len(names)} on the chart"
                                + ("; drawing many at once makes the chart hard to read"
                                   if len(self.in_view) > len(MODEL_COLORS) else ""))

    def _view_list_picked(self):
        """Apply the list's selection a moment after the last click, so a run of clicks redraws once."""
        chosen = (set(self.in_view) - set(self.view_listed)) | {
            self.view_listed[row] for row in self.view_list.curselection()}
        if self._view_job is not None:
            self.root.after_cancel(self._view_job)
            self._view_job = None
        if not chosen:
            self.view_list_note.set("Choose at least one model")
            return
        self._view_job = self.root.after(300, lambda: self._apply_view_list(chosen))

    def _apply_view_list(self, chosen):
        self._view_job = None
        self._set_view(chosen)

    def _view_listed(self, on):
        """Add every listed model to the chart, or drop them (keeping at least one model)."""
        listed = set(self.view_listed)
        chosen = set(self.in_view) | listed if on else set(self.in_view) - listed
        if not chosen:
            self.view_list_note.set("Choose at least one model")
            return
        self._set_view(chosen)

    def _apply_floor(self, chart, floor_var, note_var):
        try:
            floor = parse_floor(floor_var.get())
        except ValueError as exc:
            note_var.set(str(exc))
            return
        chart.set_floor(floor)
        self._describe_floor(chart, note_var)

    @staticmethod
    def _describe_floor(chart, note_var):
        if chart.floor is None:
            note_var.set("Blank fits the chart to its data. Values below the floor are cut off.")
        elif chart.result is None:
            note_var.set(f"Charts will start at {chart.floor:,.2f}")
        elif chart.floor_fits:
            note_var.set(f"Showing values from {chart.floor:,.2f} up")
        else:
            note_var.set(f"{chart.floor:,.2f} is above every value on this chart, so all values are shown")

    def _set_full_screen(self, on):
        """Fill the screen with the results; the controls above them return on leaving."""
        if on == self.full_screen:
            return
        self.full_screen = on
        self.root.attributes("-fullscreen", on)
        if on:
            self.controls.pack_forget()
        else:
            self.controls.pack(fill="x", before=self.picker_row)
        self.full_screen_button.configure(text="Exit full screen (Esc)" if on else "Full screen (F11)")

    def _add_comparison_tab(self):
        tab = ttk.Frame(self.results_tabs, padding=(0, 6, 0, 0))
        self.results_tabs.add(tab, text="Comparison")
        self.comparison_summary_var = tk.StringVar(value="Run a simulation to compare the selected models")
        ttk.Label(tab, textvariable=self.comparison_summary_var).pack(anchor="w", pady=(0, 6))
        hint_row = ttk.Frame(tab)
        hint_row.pack(fill="x", pady=(0, 6))
        self.chart_selected_button = ttk.Button(hint_row, text="Chart selected models",
                                                command=self._chart_selected_rows, state="disabled")
        self.chart_selected_button.pack(side="left")
        ttk.Label(hint_row, text="Click a column heading to sort or filter it, as in Excel (\u25bc marks a filtered "
                                 "column). Ctrl/Shift-click rows to select several models, then chart them (or press "
                                 "Enter). Double-click a model to open its chart. Gray rows are not in the charts.",
                  foreground="#555555").pack(side="left", padx=(8, 0))
        filter_row = ttk.Frame(tab)
        filter_row.pack(fill="x", pady=(0, 4))
        ttk.Button(filter_row, text="Clear all filters", command=self._clear_comparison_filters).pack(side="right")
        self.comparison_count_var = tk.StringVar(value="")
        ttk.Label(filter_row, textvariable=self.comparison_count_var).pack(side="right", padx=(0, 8))
        table_frame = ttk.Frame(tab)
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.comparison_table = ttk.Treeview(
            table_frame, columns=[key for key, *_ in COMPARISON_COLUMNS], show="headings", selectmode="extended"
        )
        self.comparison_table.tag_configure("hidden", foreground="#999999")
        # Each heading opens an Excel-style drop-down: sort, pick values, or a number filter.
        self.column_filters = {}
        for key, heading, width, anchor in COMPARISON_COLUMNS:
            self.comparison_table.heading(key, command=lambda key=key: self._open_column_filter(key))
            self.comparison_table.column(key, width=width, anchor=anchor, stretch=key == "model")
        self._label_headings()
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.comparison_table.xview)
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.comparison_table.yview)
        self.comparison_table.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.comparison_table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.filter_popup = None
        self.comparison_table.bind("<Double-1>", self._open_compared_model)
        self.comparison_table.bind("<Return>", lambda _event: self._chart_selected_rows())
        self.comparison_table.bind("<<TreeviewSelect>>", lambda _event: self.chart_selected_button.state(
            ["!disabled"] if self.comparison_table.selection() else ["disabled"]))
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

    def _sort_comparison(self, key, descending=None):
        """Sort by a column. Without `descending`, sorting it again reverses; text starts A-Z, numbers largest first."""
        sort_key, current = self.comparison_sort
        if descending is None:
            descending = not current if key == sort_key else key not in TEXT_COLUMNS
        self.comparison_sort = (key, descending)
        self._arrange_comparison()

    def _arrange_comparison(self):
        """Show the rows that pass every column filter, in the current sort order; hide the rest."""
        if not hasattr(self, "comparison_rows"):
            return
        key, descending = self.comparison_sort
        items = sorted(self.comparison_rows, key=lambda item: self.comparison_rows[item]["sort"][key],
                       reverse=descending)
        shown = [item for item in items if row_passes(self.comparison_rows[item], self.column_filters)]
        for item in items:
            if item not in shown and self.comparison_table.exists(item):
                self.comparison_table.detach(item)
        for position, item in enumerate(shown):
            self.comparison_table.move(item, "", position)
        total = len(self.comparison_rows)
        self.comparison_count_var.set("" if not total else f"Showing {len(shown)} of {total} models")

    def _clear_comparison_filters(self):
        self.column_filters = {}
        self._label_headings()
        self._arrange_comparison()

    def _label_headings(self):
        """Every heading shows a drop-down arrow; a filtered one shows a filled arrow and brackets."""
        for key, heading, *_ in COMPARISON_COLUMNS:
            filtered = key in self.column_filters
            self.comparison_table.heading(key, text=f"[{heading}] \u25bc" if filtered else f"{heading} \u25be")

    def set_column_filter(self, key, values=None, test=None):
        """Filter a column to `values` (shown values to keep; None for all) and/or a number `test`; neither clears it."""
        if values is None and not test:
            self.column_filters.pop(key, None)
        else:
            self.column_filters[key] = {"values": None if values is None else set(values), "test": test}
        self._label_headings()
        self._arrange_comparison()

    def _open_column_filter(self, key):
        if self.filter_popup is not None:
            self.filter_popup.close()
        self.filter_popup = ColumnFilterPopup(self, key)

    def _position_of(self, item):
        """The position in self.results of a comparison table row."""
        result = self.comparison_rows[item]["result"]
        return next(position for position, candidate in enumerate(self.results) if candidate is result)

    def _open_compared_model(self, event):
        item = self.comparison_table.identify_row(event.y)
        if item:
            self._show_model(self._position_of(item))
            self.results_tabs.select(self.chart_tab)

    def _chart_selected_rows(self):
        """Chart the models selected in the comparison table; the top selected row is the one shown."""
        selection = [item for item in self.comparison_table.get_children() if item in self.comparison_table.selection()]
        if not selection:
            return
        positions = [self._position_of(item) for item in selection]
        self._set_view(positions, current=positions[0])
        self.results_tabs.select(self.value_tab if len(positions) > 1 else self.chart_tab)

    def _set_view(self, positions, current=None):
        """Show only these models (positions in self.results) in the charts and the Showing model list.

        The model on show stays if it is still in view; otherwise `current`, or the best of
        the view by return, takes its place.
        """
        positions = sorted(set(positions))
        if not positions:
            return
        self.in_view = positions
        self.color_slots = assign_colors(self.color_slots, positions)
        if current is None:
            current = self.current if self.current in positions else max(
                positions, key=lambda position: self.results[position]["total_return"])
        self.model_picker["values"] = [self.results[position]["model_name"] for position in positions]
        shown = len(positions) == len(self.results)
        self.view_var.set(f"Charts show all {len(positions)} models" if shown else
                          f"Charts show {len(positions)} of {len(self.results)} models")
        for item in self.comparison_rows:
            self.comparison_table.item(item, tags=() if self._position_of(item) in positions else ("hidden",))
        self.current = None  # so _show_model redraws even if the model on show didn't change
        self._show_model(current)
        self._refresh_view_list()

    def _choose_models(self):
        """A dialog to pick the models in view, with a search box for long lists."""
        if not self.results:
            return
        names = [result["model_name"] for result in self.results]
        chosen = set(self.in_view)
        dialog = tk.Toplevel(self.root)
        dialog.title("Choose models to chart")
        dialog.transient(self.root)
        dialog.geometry("420x520")
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill="both", expand=True)
        search_row = ttk.Frame(frame)
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Search:").pack(side="left")
        search_var = tk.StringVar()
        search = ttk.Entry(search_row, textvariable=search_var)
        search.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(frame, text="Click to select; Ctrl/Shift-click for several. Choices outside the search are kept.",
                  foreground="#555555", wraplength=390).pack(anchor="w", pady=(6, 6))
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(list_frame, selectmode="extended", exportselection=False, activestyle="none")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        count_var = tk.StringVar()
        listed = []

        def refresh(*_):
            listed[:] = matching_models(names, search_var.get())
            listbox.delete(0, "end")
            for row, position in enumerate(listed):
                listbox.insert("end", f"{names[position]}   ({self.results[position]['total_return']:+.1%})")
                if position in chosen:
                    listbox.select_set(row)
            count_var.set(f"{len(chosen)} of {len(names)} chosen")

        def picked(*_):
            chosen.difference_update(listed)
            chosen.update(listed[row] for row in listbox.curselection())
            count_var.set(f"{len(chosen)} of {len(names)} chosen")

        def mark(on):
            (chosen.update if on else chosen.difference_update)(listed)
            refresh()

        def apply():
            if not chosen:
                count_var.set("Choose at least one model")
                return
            self._set_view(chosen)
            dialog.destroy()

        search_var.trace_add("write", refresh)
        listbox.bind("<<ListboxSelect>>", picked)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Select listed", command=lambda: mark(True)).pack(side="left")
        ttk.Button(buttons, text="Clear listed", command=lambda: mark(False)).pack(side="left", padx=(4, 0))
        ttk.Label(buttons, textvariable=count_var, foreground="#555555").pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply", command=apply).pack(side="right", padx=(0, 4))
        dialog.bind("<Return>", lambda _event: apply())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        refresh()
        search.focus_set()
        return dialog

    def _show_model(self, index):
        """Point the Chart, Trades and Metrics tabs, and the Model value highlight, at one model.

        A model outside the charts' view is added to it.
        """
        if not 0 <= index < len(self.results):
            return
        if index not in self.in_view:
            self._set_view([*self.in_view, index], current=index)
            return
        self.current = index
        result = self.results[index]
        self.model_picker.current(self.in_view.index(index))
        self.metrics_text.delete("1.0", tk.END)
        self.metrics_text.insert("1.0", f"Model: {result['model_name']}\n\n" + format_backtest_result(result))
        self.chart.show(result)
        self.value_chart.show([self.results[position] for position in self.in_view],
                              primary=self.in_view.index(index),
                              slots=[self.color_slots[position] for position in self.in_view])
        for chart, note_var in self.floor_notes:
            self._describe_floor(chart, note_var)
        self._show_trades(result)

    def _refresh_models(self):
        """List every profile in config/ and its currency folders, keeping files added from elsewhere and
        the selection. When nothing is selected yet, select each currency's best model."""
        first = not self.model_paths
        found = [str(path.resolve()) for path in profile_paths(CONFIG_DIRECTORY)]
        self.discarded_paths = set()
        if self.include_discarded_var.get():
            # Models a ranking set aside, listed after the rest and marked as discarded.
            discarded = [str(path.resolve()) for folder in discard_folders(CONFIG_DIRECTORY)
                         for path in sorted(folder.glob("*.json"))]
            self.discarded_paths = set(discarded)
            found += [path for path in discarded if path not in found]
        added = [path for path in self.model_paths if path not in found and Path(path).exists()
                 and "discard" not in Path(path).parts]
        self.model_paths = found + added
        profiles = {path: read_profile(path) or {} for path in self.model_paths}
        self.model_products = {path: profile.get("product_id", "") for path, profile in profiles.items()}
        self.regime_paths = {path for path, profile in profiles.items() if profile.get("regimes")}
        self.best_paths = {str(Path(record["path"]).resolve()) for record in read_best(CONFIG_DIRECTORY).values()}
        from model_assessor import log_rows
        listed = [path for path in self.model_paths if path not in self.discarded_paths]
        self.top_paths = top_by_history(listed, self.model_products, log_rows(self.performance_log_path),
                                        self.test_options.get("top_n", 5))
        self.selected_paths &= set(self.model_paths)
        if first:
            defaults = [path for path in self.model_paths if path in self.best_paths] or [
                path for path in self.model_paths if Path(path).stem == "btc_macro_horizon"] or self.model_paths[:1]
            self.selected_paths = set(defaults)
        currencies = sorted({product for product in self.model_products.values() if product})
        self.currency_picker["values"] = [ALL_CURRENCIES, *currencies]
        if self.currency_var.get() not in self.currency_picker["values"]:
            self.currency_var.set(ALL_CURRENCIES)
        self._fill_model_list()

    def _model_label(self, path, names):
        product = self.model_products.get(path)
        marks = ("\u2605" if path in self.best_paths else " ") + ("\u25b2 " if path in self.top_paths else "  ")
        note = ", ".join(part for part in (product, "regimes" if path in self.regime_paths else "",
                                           "discarded" if path in self.discarded_paths else "") if part)
        return f"{marks}{names[path]}" + (f"   ({note})" if note else "")

    def _passes_model_filters(self, path):
        """Whether a model passes the Currency and Type filters."""
        return model_matches(self.model_products.get(path), path in self.regime_paths, self.currency_var.get(),
                             self.model_type_var.get())

    def _fill_model_list(self):
        """Fill both lists: Available shows the unselected models of the chosen currency and type that match the
        search; Selected shows every selected model, in the order they were added."""
        search = self.model_search_var.get().lower().split()
        names = dict(zip(self.model_paths, model_names(self.model_paths)))
        self.shown_paths = [path for path in self.model_paths if path not in self.selected_paths
                            and self._passes_model_filters(path)
                            and all(word in names[path].lower() for word in search)]
        self.model_list.delete(0, "end")
        for path in self.shown_paths:
            self.model_list.insert("end", self._model_label(path, names))
        # Keep the order models were added in; any selected another way follow in library order.
        self.selection_order = [path for path in self.selection_order if path in self.selected_paths] + [
            path for path in self.model_paths if path in self.selected_paths and path not in self.selection_order]
        self.selected_list.delete(0, "end")
        for path in self.selection_order:
            self.selected_list.insert("end", self._model_label(path, names))
        listed = sum(1 for path in self.model_paths if path not in self.selected_paths)
        self.available_count_var.set(f"Available: {len(self.shown_paths)}"
                                     + (f" of {listed}" if len(self.shown_paths) != listed else ""))
        self.selected_count_var.set(f"Selected: {len(self.selection_order)}")

    def _add_models(self):
        """Move the highlighted Available models to the end of the Selected list."""
        chosen = [self.shown_paths[index] for index in self.model_list.curselection()]
        self.selected_paths |= set(chosen)
        self.selection_order += [path for path in chosen if path not in self.selection_order]
        self._fill_model_list()

    def _add_all_models(self):
        """Move every model the Available list shows (after the Currency and Type filters and search) to Selected."""
        self.model_list.select_set(0, "end")
        self._add_models()

    def _remove_models(self):
        """Move the highlighted Selected models back to Available."""
        chosen = {self.selection_order[index] for index in self.selected_list.curselection()}
        self.selected_paths -= chosen
        self._fill_model_list()

    def _remove_all_models(self):
        self.selected_paths = set()
        self._fill_model_list()

    def _move_selected(self, step):
        """Move the highlighted Selected models up or down the list."""
        indices = sorted(self.selected_list.curselection(), reverse=step > 0)
        order = list(self.selection_order)
        for index in indices:
            target = index + step
            if 0 <= target < len(order) and target not in indices:
                order[index], order[target] = order[target], order[index]
        moved = [order.index(self.selection_order[index]) for index in indices]
        self.selection_order = order
        self._fill_model_list()
        for index in moved:
            self.selected_list.select_set(index)

    def _select_top_models(self):
        """Select each currency's historically best models (only those passing the Currency and Type filters)."""
        chosen = {path for path in self.top_paths if self._passes_model_filters(path)}
        if chosen:
            self.selected_paths = chosen
            self._fill_model_list()

    def _open_test_options(self):
        from model_assessor import CONFIG_DIR  # noqa: F401  (keeps the import cost off start-up)
        from scenario_editor import TestOptionsDialog
        markets = sorted({f"{self.model_products.get(path)} {(read_profile(path) or {}).get('period_granularity', '')}"
                          for path in self.selected_model_paths() if self.model_products.get(path)})

        def saved(options):
            self.test_options = options
            self.test_mode_var.set(describe_data_mode(options))
            self._refresh_models()

        TestOptionsDialog(self.root, self.test_options, saved, markets=[m for m in markets if m.split()[-1]])

    def selected_model_paths(self):
        """The Selected list's models, in its order."""
        ordered = [path for path in self.selection_order if path in self.selected_paths]
        return ordered + [path for path in self.model_paths if path in self.selected_paths and path not in ordered]

    def select_models(self, paths):
        """Select the given profile files, adding any that aren't listed yet."""
        paths = [str(Path(path).resolve()) for path in paths]
        for path in paths:
            if path not in self.model_paths:
                self.model_paths.append(path)
                self.model_products[path] = (read_profile(path) or {}).get("product_id", "")
        self.selected_paths = set(paths)
        self.selection_order = list(dict.fromkeys(paths))
        self._fill_model_list()

    def _selected_models(self):
        """(name, profile) for each selected file; raises if nothing is selected."""
        paths = self.selected_model_paths()
        if not paths:
            raise ValueError("Select at least one model in the Models list")
        names = dict(zip(self.model_paths, model_names(self.model_paths)))
        return [(names[path], SimulationProfile.from_json(path)) for path in paths]

    def _record_best_models(self, results):
        """After a run, record and (optionally) select the best model of each currency tested.

        A currency's best is recorded in config/<product>/best_model.json only from a full-history
        run whose best beats the recorded one (see model_library.update_best). Returns a status
        summary such as "best: DOGE-USD doge_b_130 +2270%".
        """
        names = dict(zip(model_names(self.model_paths), self.model_paths))
        best = best_by_currency(results)
        summary = []
        for product, position in sorted(best.items()):
            result = results[position]
            path = names.get(result["model_name"])
            if path is not None and result.get("requested_start") is None:
                try:
                    update_best(product, Path(path).stem, path, result["total_return"], CONFIG_DIRECTORY)
                except OSError:
                    pass  # the record only saves time; never fail a finished run over it
            summary.append(f"{product} {result['model_name']} {result['total_return']:+.0%}")
        self.best_paths = {str(Path(record["path"]).resolve()) for record in read_best(CONFIG_DIRECTORY).values()}
        if self.select_best_var.get():
            chosen = {names[results[position]["model_name"]] for position in best.values()
                      if results[position]["model_name"] in names}
            if chosen:
                self.selected_paths = chosen
        self._fill_model_list()
        return "best per currency: " + ", ".join(summary)

    def _add_scenarios_tab(self):
        tab = ttk.Frame(self.results_tabs, padding=(0, 6, 0, 0))
        self.results_tabs.add(tab, text="Scenarios")
        self.scenario_note_var = tk.StringVar(
            value="Run with synthetic scenarios (Test options...) to compare models across them")
        ttk.Label(tab, textvariable=self.scenario_note_var, wraplength=1100, justify="left").pack(anchor="w",
                                                                                                pady=(0, 6))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.scenario_table = ttk.Treeview(frame, show="headings", selectmode="browse")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.scenario_table.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.scenario_table.xview)
        self.scenario_table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.scenario_table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tab

    def _add_scenario_charts_tab(self):
        """A value chart per data set: the run's data sets, by market, beside the chart of the one picked."""
        tab = ttk.Frame(self.results_tabs, padding=(0, 6, 0, 0))
        self.results_tabs.add(tab, text="Scenario charts")
        panel = ttk.Frame(tab, padding=(0, 0, 8, 0))
        panel.pack(side="left", fill="y")
        ttk.Label(panel, text="Data sets", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tree_frame = ttk.Frame(panel)
        tree_frame.pack(fill="both", expand=True, pady=(4, 0))
        tree_frame.rowconfigure(0, weight=1)
        self.scenario_chart_tree = ttk.Treeview(tree_frame, columns=("models", "best", "held"), selectmode="browse",
                                                height=20)
        for column, heading, width, anchor in (("#0", "Data set", 300, "w"), ("models", "Models", 55, "e"),
                                               ("best", "Best model", 120, "e"), ("held", "Buy & hold", 80, "e")):
            self.scenario_chart_tree.heading(column, text=heading, anchor=anchor)
            self.scenario_chart_tree.column(column, width=width, anchor=anchor, stretch=column == "#0")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.scenario_chart_tree.yview)
        sideways = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.scenario_chart_tree.xview)
        self.scenario_chart_tree.configure(yscrollcommand=scroll.set, xscrollcommand=sideways.set)
        self.scenario_chart_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        sideways.grid(row=1, column=0, sticky="ew")
        self.scenario_chart_tree.bind("<<TreeviewSelect>>", lambda event: self._show_scenario_chart())
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="\u2039 Previous", command=lambda: self._step_scenario_chart(-1)).pack(side="left")
        ttk.Button(buttons, text="Next \u203a", command=lambda: self._step_scenario_chart(1)).pack(
            side="left", padx=(4, 0))
        self.scenario_chart_note = tk.StringVar(value="Run a simulation to chart each data set it tested on")
        ttk.Label(panel, textvariable=self.scenario_chart_note, foreground="#555555", wraplength=520,
                  justify="left").pack(anchor="w", pady=(4, 0))

        figure = Figure(figsize=(9, 5), dpi=100)
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab, pack_toolbar=False)
        self.scenario_chart = ModelValueChart(figure, canvas)
        self.scenario_chart.tax_panel = False  # the value gets the height; the Model value tab has the tax
        toolbar.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.scenario_chart_groups, self.scenario_chart_items = [], {}
        return tab

    def _fill_scenario_charts(self, results):
        """List the run's data sets (see scenario_groups) by market, then data source, then path, and chart
        the first. A source with several paths is one row, with its paths under it."""
        self.scenario_chart_groups = groups = scenario_groups(results)
        models = list(dict.fromkeys(result.get("base_model", result["model_name"]) for result in results))
        self.scenario_chart_slots = {model: slot for slot, model in enumerate(models)}  # one color per model
        tree = self.scenario_chart_tree
        tree.delete(*tree.get_children())
        self.scenario_chart_items = {}  # tree item -> group index (a source with paths: its first path)
        self.scenario_chart_order = []  # the items of every data set, in order, for Previous and Next

        def figures(indices):
            bests = sorted(max(results[p]["total_return"] for p in groups[i]["positions"]) for i in indices)
            held = sorted(results[groups[i]["positions"][0]]["buy_and_hold_return"] for i in indices)
            if len(indices) == 1:
                return f"{bests[0]:+.0%}", f"{held[0]:+.0%}"
            return (f"{bests[len(bests) // 2]:+.0%} (worst {bests[0]:+.0%})", f"{held[len(held) // 2]:+.0%}")

        markets = list(dict.fromkeys(group["market"] for group in groups))
        for number, market in enumerate(markets):
            market_item = tree.insert("", "end", text=market, open=True)
            sources = list(dict.fromkeys(group["source"] for group in groups if group["market"] == market))
            for source in sources:
                indices = [i for i, group in enumerate(groups) if group["market"] == market
                           and group["source"] == source]
                models_count = len(groups[indices[0]]["positions"])
                best, held = figures(indices)
                name = source if len(indices) == 1 else f"{source} ({len(indices)} paths)"
                item = tree.insert(market_item, "end", text=name, values=(models_count, best, held))
                self.scenario_chart_items[item] = indices[0]
                if len(indices) == 1:
                    self.scenario_chart_order.append(item)
                    continue
                for index in indices:
                    best, held = figures([index])
                    path_item = tree.insert(item, "end", text=f"path #{groups[index]['path'] + 1}",
                                            values=(models_count, best, held))
                    self.scenario_chart_items[path_item] = index
                    self.scenario_chart_order.append(path_item)
        synthetic_sets = sum(group["source"] != HISTORIC for group in groups)
        self.scenario_chart_note.set(
            f"{len(groups)} data sets ({synthetic_sets} synthetic) on {len(markets)} "
            f"market{'s' if len(markets) != 1 else ''}. The chart shows every model's value on the data set picked, "
            "against buying and holding through it; a dashed line marks where a scenario starts. Best model: its "
            "return (for several paths, the median, with the worst path). Click a scenario with paths to see its "
            "first; Previous and Next step through every data set.")
        if self.scenario_chart_order:
            self._pick_scenario_chart(self.scenario_chart_order[0])

    def _pick_scenario_chart(self, item):
        tree = self.scenario_chart_tree
        parent = tree.parent(item)
        while parent:
            tree.item(parent, open=True)
            parent = tree.parent(parent)
        tree.selection_set(item)
        tree.see(item)

    def _step_scenario_chart(self, step):
        if not self.scenario_chart_order:
            return
        selection = self.scenario_chart_tree.selection()
        current = selection[0] if selection else self.scenario_chart_order[0]
        if current not in self.scenario_chart_order:  # a source with paths, or a market: start from its first
            index = self.scenario_chart_items.get(current)
            current = next((item for item in self.scenario_chart_order
                            if self.scenario_chart_items[item] == index), self.scenario_chart_order[0])
        position = self.scenario_chart_order.index(current)
        position = min(max(position + step, 0), len(self.scenario_chart_order) - 1)
        self._pick_scenario_chart(self.scenario_chart_order[position])
        self.root.update_idletasks()
        self._show_scenario_chart()

    def _show_scenario_chart(self):
        """Chart every model run on the picked data set (for a market row, its first)."""
        selection = self.scenario_chart_tree.selection()
        if not selection or not self.scenario_chart_groups:
            return
        item = selection[0]
        if item not in self.scenario_chart_items:  # a market row
            children = self.scenario_chart_tree.get_children(item)
            if not children:
                return
            item = children[0]
        group = self.scenario_chart_groups[self.scenario_chart_items[item]]
        runs = [self.results[position] for position in group["positions"]]
        best = max(range(len(runs)), key=lambda index: runs[index]["total_return"])
        marks = [(group["start"], "Scenario starts")] if group["start"] is not None else []
        self.scenario_chart.show(
            runs, primary=best,
            slots=[self.scenario_chart_slots.get(run.get("base_model", run["model_name"]), 0) for run in runs],
            heading=group["label"], marks=marks, legend_location="best")  # data sets differ in shape

    def _show_scenarios(self, results):
        """Fill the Scenarios tab: one row per model, one column per data source, most robust first."""
        sources, rows = scenario_matrix(results)
        columns = ["model", "product", *[f"s{index}" for index in range(len(sources))], "worst", "beat"]
        self.scenario_table.delete(*self.scenario_table.get_children())
        self.scenario_table["columns"] = columns
        headings = {"model": ("Model", 200, "w"), "product": ("Currency", 80, "w"),
                    "worst": ("Worst scenario", 110, "e"), "beat": ("Beat buy & hold", 110, "e")}
        for index, source in enumerate(sources):
            headings[f"s{index}"] = (source[:40], 170 if source != HISTORIC else 90, "e")
        for key in columns:
            heading, width, anchor = headings[key]
            self.scenario_table.heading(key, text=heading)
            self.scenario_table.column(key, width=width, anchor=anchor, stretch=False)

        def cell(value):
            if value is None:
                return ""
            middle, worst, count = value
            return f"{middle:+.1%}" if count == 1 else f"{middle:+.1%} (worst {worst:+.1%}, {count} paths)"

        for row in rows:
            self.scenario_table.insert("", "end", values=[
                row["model"], row["product"], *[cell(row["cells"].get(source)) for source in sources],
                "" if row["worst"] is None else f"{row['worst']:+.1%}",
                f"{row['beat']} of {row['tested']}" if row["tested"] else ""])
        if len(sources) > 1:
            self.scenario_note_var.set(
                "Return in each scenario, measured from the scenario's start (for scenarios with several paths: the "
                "median, with the worst path). Most robust first: sorted by each model's worst scenario. \"Beat "
                "buy & hold\" counts the scenario runs where the model ended above buying and holding through it.")
        else:
            self.scenario_note_var.set("Run with synthetic scenarios (Test options...) to compare models across them")

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

    def _open_clear_data(self):
        """The Clear data dialog (data_manager.py): clear stored price history or synthetic candles."""
        from data_manager import ClearDataDialog
        self.clear_data_dialog = ClearDataDialog(
            self.root, busy=lambda: self.simulation_thread is not None and self.simulation_thread.is_alive())

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
            mode = self.test_options.get("mode", "historic")
            # One model on one set of data per worker at a time: history runs a task per model, scenario
            # and stress runs a task per model per path, spread over every worker. Auto: every CPU but two
            # (scenario runs add the GPUs).
            workers = parse_workers(self.workers_var.get()) if self.parallel_var.get() else 1
            # A new budget each run, measured against the memory free at launch.
            self.memory_guard = MemoryGuard(parse_memory_percent(self.memory_var.get()), baseline=self.launch_free_memory)
            scenarios = synthetic.load_set(self.test_options["scenario_set"]) if mode in (
                "historic+synthetic", "synthetic") else []
            if mode in ("historic+synthetic", "synthetic") and not scenarios:
                raise ValueError("The scenario set is empty: add scenarios in Test options, or test historic data only")
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

        self._show_workers([])
        for phase in self.progress_bars:
            self.progress_bars[phase]["value"] = 0
            self.progress_vars[phase].set("waiting")
        self.metrics_text.delete("1.0", tk.END)
        self.run_button.state(["disabled"])
        self.result_var.set("Simulation status: testing the Coinbase API")
        # Not `mode`: that is the data mode the worker needs. Scenario and stress runs make a task per model per path,
        # shared across every worker; history runs a task per model.
        run_kind = describe_mode(workers, len(models) if mode == "historic" else workers)
        self.run_timing = {"started": time.monotonic(), "mode": run_kind, "models": [name for name, _ in models],
                           "kind": "parallel" if run_kind.startswith("parallel") else "single"}
        self._show_time()

        self.simulation_thread = threading.Thread(
            target=self._simulation_worker, args=(client, models, start_time, self.fresh_signal_var.get(), workers,
                                                  mode, scenarios),
            daemon=True,
        )
        self.simulation_thread.start()
        self.root.after(100, self._poll_progress)

    def _simulation_worker(self, client, models, start_time=None, wait_for_fresh_signal=False, workers=1,
                           mode="historic", scenarios=()):
        """Test the API, then gather data, simulate each (name, profile) in turn and log the results.

        With `start_time`, every model starts testing there (see run_backtest). With more than one
        of `workers` and of models, each model runs on its history in its own worker process (see
        _parallel_results). With `mode` "historic+synthetic" or "synthetic", each model is also (or
        only) run on every scenario path, a task per model per path on `workers` processes (see
        _synthetic_runs); synthetic results are never logged or recorded as a currency's best.
        Runs off the Tk thread, so it only posts events for _poll_progress to apply.
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
            results, logged, candles_by_market = [], "", {}
            if mode == "stress":
                self._stress_test(client, models, frictions, wait_for_fresh_signal, workers)
                return
            if mode != "synthetic" and workers > 1 and len(models) > 1:
                outcome = self._parallel_results(client, models, inputs, start_time, wait_for_fresh_signal, workers)
                if outcome is None:
                    return
                results, logged = outcome
            elif mode != "synthetic":
                results, logged, candles_by_market = self._serial_results(
                    client, models, frictions, start_time, wait_for_fresh_signal)
            if mode != "historic":
                if not candles_by_market:
                    candles_by_market = self._gather_markets(client, models)
                results = results + self._synthetic_runs(models, frictions, candles_by_market, scenarios,
                                                         wait_for_fresh_signal, workers)
                logged = (logged + "; " if logged else "") + "synthetic results aren't logged"
            post(("done", results, join_notes(logged, self._run_guard().describe())))
        except Exception as exc:  # Reported in the window; the worker must not die silently.
            post(("error", exc))

    def _run_guard(self):
        """This run's memory guard (see memory_guard.py): the one Run Simulation made, or a default one."""
        if getattr(self, "memory_guard", None) is None:
            self.memory_guard = MemoryGuard(DEFAULT_MEMORY_PERCENT, baseline=getattr(self, "launch_free_memory", None))
        return self.memory_guard

    def _gather_markets(self, client, models):
        """{(product, granularity): candles} for the models' markets, downloading what the cache lacks."""
        post = self.progress_queue.put
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
        return candles_by_market

    def _serial_results(self, client, models, frictions, start_time, wait_for_fresh_signal):
        """Run each model on its market's history in turn and log the results: (results, logged, candles)."""
        post = self.progress_queue.put
        candles_by_market = self._gather_markets(client, models)
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
            self._run_guard().admit([result], results)  # chart data to disk as memory fills
        path = self.performance_log_path
        try:
            logged = describe_update(update_performance_log(
                [(result, profile) for result, (_, profile) in zip(results, models)], path), path)
        except OSError as exc:  # e.g. the CSV is open in Excel; the results still show
            logged = f"{path.name} not updated ({exc})"
        return results, logged, candles_by_market

    def _synthetic_runs(self, models, frictions, candles_by_market, scenarios, wait_for_fresh_signal, workers=1):
        """Run every model on every path of every scenario, built from its market's history.

        Each model on each path is one task, shared out to `workers` processes (and the GPUs) in
        parallel mode (see stress_test.run_scenarios). A result is named "<model> | <scenario>"
        (with " #n" for a scenario's paths) and measured from the scenario's first candle.
        """
        import stress_test
        post = self.progress_queue.put
        results, skipped = stress_test.run_scenarios(
            models, frictions, candles_by_market, scenarios, wait_for_fresh_signal=wait_for_fresh_signal,
            workers=workers, compute_device="auto", worker_status=lambda rows: post(("workers", rows)),
            synthetic_dir=synthetic.SYNTHETIC_DIR, memory_guard=self._run_guard(),
            progress=lambda fraction, message: post(("progress", "simulation", fraction, message)))
        for note in skipped:
            post(("status", f"Simulation status: skipped {note}"))
        return results

    def _stress_test(self, client, models, frictions, wait_for_fresh_signal, workers):
        """Run the stress test (stress_test.py) on `workers` processes (and the GPUs, in parallel mode),
        write its reports, and post the results and the report."""
        import stress_test
        post = self.progress_queue.put
        candles_by_market = self._gather_markets(client, models)
        # The saved stress plan (scenarios/stress_plan.json), the same every run until it is edited.
        combos = stress_test.plan_combinations(stress_test.load_plan(), self.test_options.get("stress_paths", 3))
        post(("status", f"Simulation status: stress testing {len(models)} models on {len(combos)} combinations"))
        records, baselines, results = stress_test.run_stress(
            models, frictions, candles_by_market, combos, wait_for_fresh_signal=wait_for_fresh_signal,
            workers=workers, compute_device="auto", worker_status=lambda rows: post(("workers", rows)),
            synthetic_dir=synthetic.SYNTHETIC_DIR, memory_guard=self._run_guard(),
            progress=lambda fraction, message: post(("progress", "simulation", fraction, message)))
        for result in results:
            result["stress_test"] = True
        settings = (f"stress plan of {len(combos)} combinations, up to {self.test_options.get('stress_paths', 3)} "
                    f"paths, alone and with history")
        index = stress_test.write_reports(records, baselines, dict(models), settings)
        post(("report", index))
        post(("done", results, join_notes(f"stress test of {len(records)} runs; report {index}, "
                                          f"and as a PDF {stress_test.report_pdf(index)}",
                                          self._run_guard().describe())))

    def _parallel_results(self, client, models, friction_inputs, start_time, wait_for_fresh_signal, workers):
        """Run the models on worker processes; (results, logged), or None after posting the error."""
        post = self.progress_queue.put
        post(("status", f"Simulation status: running {len(models)} models on {min(workers, len(models))} workers"))
        # CPU only: history runs read their indicators from the candle store, so GPU workers only add load.
        run = run_parallel(client, models, workers=workers, start_time=start_time, compute_device="cpu",
                           wait_for_fresh_signal=wait_for_fresh_signal, friction_inputs=friction_inputs,
                           performance_path=self.performance_log_path, memory_guard=self._run_guard(),
                           progress=lambda phase, fraction, message: post(("progress", phase, fraction, message)))
        results = [result for result in run["results"] if result is not None]
        failed = "; ".join(f"{name}: {error}" for name, error in run["errors"].items())
        if not results:
            post(("error", RuntimeError(f"Every model failed - {failed}")))
            return None
        return results, run["logged"] + (f"; FAILED {failed}" if failed else "")

    def _poll_progress(self):
        """Apply the worker's queued events to the window; checks again every 100 ms until the run ends."""
        finished = succeeded = False
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
            elif kind == "workers":
                self._show_workers(event[1])
            elif kind == "report":
                import webbrowser
                self.last_report = event[1]
                webbrowser.open(Path(event[1]).as_uri())
            elif kind == "done":
                self.results, logged = event[1], event[2]
                # The earlier run's chart data on disk belongs to results no longer shown.
                clean_spills(keep=[self.memory_guard.spill_folder] if self.memory_guard is not None else [])
                best = max(range(len(self.results)), key=lambda i: self.results[i]["total_return"])
                self._show_comparison(self.results)
                self._show_scenarios(self.results)
                self._fill_scenario_charts(self.results)
                self.color_slots = {}
                historic = [result for result in self.results if is_historic(result)]
                per_currency = {product: self.results.index(historic[position])
                                for product, position in best_by_currency(historic).items()}
                # Several currencies: chart each one's best; one currency: its best few.
                view = list(per_currency.values()) if len(per_currency) > 1 else default_view(self.results)
                self._set_view(view, current=best)
                if historic:
                    logged = f"{self._record_best_models(historic)}; {logged}"
                if len(self.results) == 1:
                    result = self.results[0]
                    self.results_tabs.select(self.chart_tab)
                    self.result_var.set(f"Simulation status: complete ({result['total_return']:+.2%} over "
                                        f"{result['trade_count']:,} trades); {logged}")
                else:
                    self.results_tabs.select(self.comparison_tab)
                    self.result_var.set(
                        f"Simulation status: complete, {len(self.results)} models; best "
                        f"{self.results[best]['model_name']} {self.results[best]['total_return']:+.2%}; {logged}")
                finished = succeeded = True
            elif kind == "error":
                self.result_var.set(f"Simulation status: error - {event[1]}")
                if messagebox is not None:
                    messagebox.showerror("Simulation Run Error", str(event[1]))
                finished = True

        self._show_time(finished=finished, succeeded=succeeded)
        if finished:
            self._show_workers([])
            self.run_button.state(["!disabled"])
        else:
            self.root.after(100, self._poll_progress)

    def _show_workers(self, rows):
        """Show what each stress test worker is running (rows from stress_test.run_stress), or hide the table."""
        if not rows:
            self.workers_table.delete(*self.workers_table.get_children())
            self.workers_label.grid_remove()
            self.workers_table.grid_remove()
            return
        self.workers_label.grid()
        self.workers_table.grid()
        self.workers_table.configure(height=min(len(rows), 16))
        for row in rows:
            item, values = f"worker{row['worker']}", format_worker_row(row)
            if self.workers_table.exists(item):
                self.workers_table.item(item, values=values)
            else:
                self.workers_table.insert("", "end", iid=item, values=values)

    def _show_time(self, finished=False, succeeded=False):
        """Update the Time spent line: running time during a run, then the total once it ends.

        The time runs from clicking Run Simulation to the results appearing, so it includes
        the API check and data gathering, which take the same time in either mode.
        """
        timing = self.run_timing
        if timing is None:
            return
        seconds = time.monotonic() - timing["started"]
        models = f"{len(timing['models'])} model{'s' if len(timing['models']) != 1 else ''}"
        if not finished:
            self.time_var.set(f"{format_elapsed(seconds)} so far ({models}, {timing['mode']})")
            return
        self.run_timing = None
        if not succeeded:
            self.time_var.set(f"{format_elapsed(seconds)} until the run stopped with an error ({timing['mode']})")
            return
        self.time_var.set(f"{format_elapsed(seconds)} for {models} ({timing['mode']})")
        self.timings[timing["kind"]] = {"seconds": seconds, "models": timing["models"], "mode": timing["mode"]}
        self.timing_compare_var.set(compare_timings(self.timings) or
                                    "Run the same models in the other mode to compare the times")


def main(argv=None):
    """Open the window; `--models PATH ...` selects those profiles (as Model Lab's "Open in simulator" does)."""
    import argparse
    parser = argparse.ArgumentParser(description="The backtest simulator window.")
    parser.add_argument("--models", nargs="+", metavar="PATH", help="profiles to select when the window opens")
    args = parser.parse_args(argv)
    if tk is None:
        raise RuntimeError("tkinter is required for the simulator GUI.")

    root = tk.Tk()
    window = SimulationWindow(root)
    if args.models:
        window.select_models([path for path in args.models if Path(path).exists()])
    root.mainloop()


if __name__ == "__main__":
    main()

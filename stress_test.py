"""Stress tests: push models through combinations of scenarios and nightmares, with and without history,
and write a detailed report for each model.

Run from the simulator (Test options... > "Stress test"). For each tested model:

  1. its baseline: the real history alone;
  2. every scenario run of the chosen set and every nightmare, on its own;
  3. a seeded sample of ordered pairs of them (one straight after the other);

each of 2-3 measured two ways: "alone" from the scenario's first candle, and "with history" over
the full real history with the scenario appended. A scenario with several paths contributes up
to `max_paths` of them. Reports go to data/stress_reports/<time>/: index.html ranks the models
by their worst result, <model>.html details each one, and results.csv holds every run.

Nothing here is logged to the performance log or recorded as a currency's best model.
"""

import base64
import collections
import csv
import html
import io
import json
import itertools
import math
import os
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import synthetic

REPORT_DIR = Path(__file__).parent / "data" / "stress_reports"
NIGHTMARE_SET = synthetic.SCENARIO_DIR / "nightmares.json"
NIGHTMARES = [
    {"name": "Nightmare: exchange collapse, -80% in 2 days", "type": "crash", "placement": "append", "drop": 0.8,
     "days": 2, "recovery_days": 10, "recovery": 0.05, "volatility": 1.5, "seed": 101},
    {"name": "then bleeds -40% over 6 months", "type": "bear", "chain": True, "drop": 0.4, "days": 180,
     "volatility": 1.2, "seed": 102},
    {"name": "Nightmare: euphoria +300% in a month, then a -90% rug pull", "type": "pump", "placement": "append",
     "rise": 3.0, "days": 30, "drop": 0.9, "collapse_days": 7, "volatility": 1.5, "seed": 103},
    {"name": "Nightmare: whipsaw hell, +-35% at 3x volatility for 6 months", "type": "sideways", "placement": "append",
     "range": 0.35, "days": 180, "volatility": 3.0, "seed": 104},
    {"name": "Nightmare: death spiral, -95% over 2 years", "type": "bear", "placement": "append", "drop": 0.95,
     "days": 730, "volatility": 1.3, "seed": 105},
    {"name": "Nightmare: crash cascade, -35%", "type": "crash", "placement": "append", "drop": 0.35, "days": 4,
     "recovery_days": 25, "recovery": 0.5, "volatility": 1.3, "seed": 106},
    {"name": "-35% again", "type": "crash", "chain": True, "drop": 0.35, "days": 4, "recovery_days": 25,
     "recovery": 0.5, "volatility": 1.3, "seed": 107},
    {"name": "-35% a third time", "type": "crash", "chain": True, "drop": 0.35, "days": 4, "recovery_days": 25,
     "recovery": 0.3, "volatility": 1.3, "seed": 108},
]
MODES = ("alone", "with history")


def load_nightmares(path=None):
    """The nightmare scenario set; written with the five built-in nightmares the first time."""
    path = Path(path or NIGHTMARE_SET)
    if not path.exists():
        synthetic.save_set(NIGHTMARES, path)
    return synthetic.load_set(path)


def combinations(scenarios, nightmares, pairs=20, max_paths=3, seed=1):
    """The stress runs: [(label, modules, is_nightmare, paths)]. Every scenario run and nightmare alone, then
    `pairs` ordered pairs of them sampled with `seed` (the second continuing from the first)."""
    units = [(run, False) for run in synthetic.runs(list(scenarios))] + [(run, True) for run in synthetic.runs(list(nightmares))]
    combos = [(synthetic.run_label(run), run, nightmare, min(synthetic.run_paths(run), max_paths))
              for run, nightmare in units]
    ordered = [(a, b) for a, b in itertools.permutations(range(len(units)), 2)]
    for first, second in random.Random(seed).sample(ordered, min(pairs, len(ordered))):
        (run_a, nightmare_a), (run_b, nightmare_b) = units[first], units[second]
        # The second run continues the first: its first module is chained on.
        chained = [dict(module) for module in run_a] + [dict(run_b[0], chain=True)] + [dict(m) for m in run_b[1:]]
        label = f"{synthetic.run_label(run_a)} -> {synthetic.run_label(run_b)}"
        combos.append((label, chained, nightmare_a or nightmare_b, min(synthetic.run_paths(chained), max_paths)))
    return combos


# --- the stress plan: the saved list of combinations every stress test runs ---------------------

PLAN_FILE = synthetic.SCENARIO_DIR / "stress_plan.json"
DEFAULT_PAIRS = 20


def default_plan():
    """The baseline plan: every built-in scenario run and nightmare alone, plus DEFAULT_PAIRS fixed pairs."""
    scenarios = [synthetic.normalize(s) for s in synthetic.DEFAULT_SCENARIOS]
    nightmares = [synthetic.normalize(s) for s in NIGHTMARES]
    return [{"name": label, "nightmare": nightmare, "modules": run}
            for label, run, nightmare, _ in combinations(scenarios, nightmares, pairs=DEFAULT_PAIRS, seed=1)]


def normalize_plan(plan):
    """The plan with each combination's modules checked and a run made of them (the first starts it, the rest
    are chained on); raises ScenarioError for a bad module or an empty combination."""
    out = []
    for number, combination in enumerate(plan, start=1):
        modules = combination.get("modules") or []
        if not modules:
            raise synthetic.ScenarioError(f"combination {number} has no scenario modules")
        modules = [synthetic.normalize({**module, "chain": index > 0}) for index, module in enumerate(modules)]
        name = combination.get("name") or synthetic.run_label(modules)
        nightmare = bool(combination.get("nightmare", any(m["name"].startswith("Nightmare") for m in modules)))
        out.append({"name": name, "nightmare": nightmare, "modules": modules})
    return out


def save_plan(plan, path=None):
    path = Path(path or PLAN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"combinations": normalize_plan(plan)}, indent=2) + "\n", encoding="utf-8")


def load_plan(path=None):
    """The stress plan; the default plan is written the first time, then the saved one is always used."""
    path = Path(path or PLAN_FILE)
    if not path.exists():
        save_plan(default_plan(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return normalize_plan(data.get("combinations", []) if isinstance(data, dict) else data)


def plan_combinations(plan, max_paths=3):
    """The plan as run_stress's combinations: [(label, modules, is_nightmare, paths)]."""
    return [(c["name"], c["modules"], c["nightmare"], min(synthetic.run_paths(c["modules"]), max_paths))
            for c in plan]


def _downsample(values, points=1500):
    values = list(values)
    if len(values) <= points:
        return values
    step = len(values) / points
    return [values[int(i * step)] for i in range(points)] + [values[-1]]


def _record(model, product, granularity, label, mode, path, nightmare, result):
    growth, held = 1 + result["total_return"], 1 + result["buy_and_hold_return"]
    return {
        "model": model, "product": product, "candles": granularity, "scenario": label, "mode": mode, "path": path,
        "nightmare": nightmare, "return": result["total_return"], "buy_and_hold": result["buy_and_hold_return"],
        "vs_buy_and_hold": growth / held - 1 if held > 0 else float("inf"),
        "lost_money": result["total_return"] < 0, "beat_buy_and_hold": result["total_return"] > result["buy_and_hold_return"],
        "max_drawdown": result["max_drawdown"], "trades": result["trade_count"], "win_rate": result["win_rate"],
        "fees": result["fees_paid"], "taxes": result["taxes_paid"], "ending_value": result["ending_equity"],
        "starting_capital": result["starting_capital"],
        "equity": _downsample(result["series"]["equity"]), "close": _downsample(result["series"]["close"]),
    }


CHART_POINTS = 5000  # the most candles of a stress result's series kept for the simulator's charts

_histories = {}  # this process's real candles for each market, {(product, granularity): candles}
_status = []  # this process's [queue] for what it is running (see _say), when the caller asked


def _say(doing, fraction=0.0):
    """Tell the caller what this process is running and how far through it is (None: between tasks)."""
    if _status:
        _status[0].put((os.getpid(), doing, fraction))


def _watch(doing):
    """A run_backtest progress callback that passes every 5% on to _say."""
    _say(doing, 0.0)
    last = [0]

    def progress(fraction, message):
        step = int(fraction * 20)
        if step != last[0]:
            last[0] = step
            _say(doing, fraction)
    return progress


def _gpu(device):
    return f" [GPU {device[1]}]" if device[0] == "cuda" else ""


def _trim(result):
    """Keep the last CHART_POINTS of each series, as copies, so the full-length arrays can be freed."""
    series = result["series"]
    for key, values in list(series.items()):
        if key == "indicators":
            series[key] = {name: list(v[-CHART_POINTS:]) for name, v in values.items()}
        else:
            series[key] = list(values[-CHART_POINTS:])
    return result


def _baseline_task(market, name, profile, friction, wait, device):
    """The model on its market's real history: (record, result)."""
    from backtest import run_backtest
    from candle_store import MarketCandles
    product, granularity, _ = market
    history = _histories[(product, granularity)]
    candles = history if hasattr(history, "metrics") else MarketCandles(history, synthetic.MemoryMetrics())
    result = run_backtest(candles, profile, friction=friction, wait_for_fresh_signal=wait,
                          compute_device=device[0], device_id=device[1],
                          progress=_watch(f"{name}: real history{_gpu(device)}"))
    _say(None)
    record = _record(name, product, granularity, "Historic baseline", "history", 0, False, result)
    result.update(model_name=name, granularity=granularity, base_model=name, data_source="Historic")
    return record, result  # the whole history, as the simulator charts real runs


def _combo_task(market, label, run, nightmare, path, paths, name, profile, friction, modes, wait, style,
                synthetic_dir, device):
    """One model on one path of one combination, in every mode: [(record, result)], or the
    synthetic.ScenarioError's message when the scenario doesn't fit the history.

    With `style` "stress", results are named "<model> | <run> (<mode>)" and trimmed to CHART_POINTS;
    with "scenario" (the simulator's synthetic data), "<model> | <run>", whole, as before stress tests.
    With `synthetic_dir`, the run's candles are kept in its synthetic.SyntheticStore, apart from the history.
    """
    from backtest import run_backtest
    product, granularity, seconds = market
    name_of_run = label + (f" #{path + 1}" if paths > 1 else "")
    _say(f"{name}: building {name_of_run}{_gpu(device)}")
    try:
        store = synthetic.SyntheticStore(synthetic_dir) if synthetic_dir is not None else None
        candles, start = synthetic.market_candles(list(_histories[(product, granularity)]), run, seconds, path,
                                                  store=store, market=(product, granularity))
    except synthetic.ScenarioError as exc:
        _say(None)
        return str(exc)
    done = []
    for mode in modes:
        shown = f" ({mode})" if style == "stress" else ""
        result = run_backtest(candles, profile, friction=friction,
                              start_time=start if mode == "alone" else None, wait_for_fresh_signal=wait,
                              compute_device=device[0], device_id=device[1],
                              progress=_watch(f"{name}: {name_of_run}{shown}{_gpu(device)}"))
        record = _record(name, product, granularity, name_of_run, mode, path, nightmare, result)
        if style == "stress":
            result.update(model_name=f"{name} | {name_of_run} ({mode})", granularity=granularity,
                          base_model=name, data_source=f"{label} ({mode})", scenario_path=path,
                          scenario_start=start)
            result = _trim(result)
        else:
            result.update(model_name=f"{name} | {name_of_run}", granularity=granularity, base_model=name,
                          data_source=label, scenario_path=path, scenario_start=start)
        done.append((record, result))
    _say(None)
    return done


def _start_worker(histories, status=None):
    _histories.clear()
    _histories.update(histories)
    _status[:] = [status] if status is not None else []


class _Direct:
    """A stand-in for the status queue when the runs share the caller's thread."""

    def __init__(self, handle):
        self.put = handle


def _tasks(models, frictions, combos, modes, wait, compute_device="cpu", gpu_devices=(), baseline=True,
           style="stress", synthetic_dir=None):
    """[(function, args, runs it makes)] in report order: for each market, its models' baselines (with
    `baseline`), then for every path of every combination, a task per model.

    Tasks take turns on the GPUs, when there are any.
    """
    from backtest import resolve_granularity
    by_market = {}
    for name, profile in models:
        granularity, seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        by_market.setdefault((profile["product_id"], granularity, seconds), []).append((name, profile))
    tasks = []
    for market, members in by_market.items():
        for name, profile in members if baseline else ():
            tasks.append((_baseline_task, [market, name, profile, frictions[name], wait], 1))
        for label, run, nightmare, paths in combos:
            for path in range(paths):
                for name, profile in members:
                    tasks.append((_combo_task, [market, label, run, nightmare, path, paths, name, profile,
                                                frictions[name], modes, wait, style, synthetic_dir],
                                  len(modes)))
    for number, (_, args, _) in enumerate(tasks):
        args.append((compute_device, gpu_devices[number % len(gpu_devices)]) if gpu_devices else ("cpu", 0))
    return tasks


def _model_of(task):
    """The model a task runs."""
    function, args, _ = task
    return args[1] if function is _baseline_task else args[6]


def _next_tasks(queues, running, free):
    """Up to `free` task numbers to start, each the next task of the model with the fewest tasks `running`
    (a Counter of tasks running per model, updated here), taking tied models in turn.

    While several models have tasks left this spreads the work one model per worker; once fewer models
    than workers are left, idle workers take more of the same model's tasks rather than waiting.
    `queues` maps each model to its task numbers still to run, in order, and is consumed."""
    chosen = []
    while len(chosen) < free:
        waiting = [model for model, queue in queues.items() if queue]
        if not waiting:
            break
        model = min(waiting, key=lambda name: running[name])  # min keeps the first of a tie: models in turn
        chosen.append(queues[model].pop(0))
        running[model] += 1
    return chosen


def _describe(task):
    function, args, _ = task
    if function is _baseline_task:
        return f"{args[1]}: history"
    return f"{args[6]}: {args[1]}" + (f" #{args[4] + 1}" if args[5] > 1 else "")


def run_stress(models, frictions, candles_by_market, combos, modes=MODES, wait_for_fresh_signal=False,
               progress=None, stop=None, workers=1, compute_device="cpu", worker_status=None, synthetic_dir=None):
    """Run every model through every combination and mode. Returns (records, baselines, results).

    `models` are (name, profile); `candles_by_market` maps (product, granularity) to history.
    `progress(fraction, message)` reports; `stop()` returning True ends early. `results` are the
    backtest results (series trimmed to CHART_POINTS) for the simulator's tables and charts.

    Each model's run on its real history, and on each path of each combination (in every mode),
    is one task. With `workers` above 1, the tasks are shared out to that many worker processes,
    plus one per GPU when `compute_device` is "auto" or "cuda" and CUDA devices are visible
    (supported indicators are then computed on the GPUs), but never more than there are tasks. Each
    worker runs one task at a time on one thread. Free workers take the next task of the model with the
    fewest running, so the models are spread across the workers, and when fewer models than workers have
    tasks left, a model's tasks run on several workers at once instead of leaving workers idle. The
    records come back in the same order either way.

    `worker_status(rows)`, when given, is called as the workers' activity changes (at most four
    times a second): one row per worker, {"worker": 1.., "doing": what it is running, or None
    between tasks, "fraction": how far through that run}.

    With `synthetic_dir` (such as synthetic.SYNTHETIC_DIR), every scenario path's candles are kept
    in that folder's synthetic.SyntheticStore, apart from the real history.
    """
    records, baselines, results, _ = _run(models, frictions, candles_by_market, combos, modes,
                                          wait_for_fresh_signal, progress, stop, workers, compute_device,
                                          worker_status, baseline=True, style="stress", synthetic_dir=synthetic_dir)
    return records, baselines, results


def run_scenarios(models, frictions, candles_by_market, scenarios, wait_for_fresh_signal=False, progress=None,
                  stop=None, workers=1, compute_device="cpu", worker_status=None, synthetic_dir=None):
    """The simulator's synthetic data: every model on every path of every run of `scenarios` (a scenario set;
    chained modules make one run, see synthetic.runs), measured from the run's first candle.

    Returns (results, skipped): results named "<model> | <run>" (" #n" for a run's paths), and
    "<run>: <reason>" for each run that doesn't fit a market's history. A task per model per path;
    `workers` and the rest as for run_stress.
    """
    combos = [(synthetic.run_label(run), run, False, synthetic.run_paths(run)) for run in synthetic.runs(list(scenarios))]
    _, _, results, skipped = _run(models, frictions, candles_by_market, combos, ("alone",), wait_for_fresh_signal,
                                  progress, stop, workers, compute_device, worker_status, baseline=False,
                                  style="scenario", synthetic_dir=synthetic_dir)
    return results, skipped


def _run(models, frictions, candles_by_market, combos, modes, wait_for_fresh_signal, progress, stop, workers,
         compute_device, worker_status, baseline, style, synthetic_dir=None):
    """run_stress and run_scenarios: (records, baselines, results, skipped)."""
    gpu_devices = []
    if workers > 1:
        from parallel_backtest import resolve_compute_device
        compute_device, gpu_devices = resolve_compute_device(compute_device)
    else:
        compute_device = "cpu"
    tasks = _tasks(models, frictions, combos, modes, wait_for_fresh_signal, compute_device, gpu_devices,
                   baseline, style, synthetic_dir)
    # Tasks are independent (each builds its own scenario candles), so a model's tasks can share the workers.
    workers = max(1, min(len(tasks), workers + len(gpu_devices) if workers > 1 else 1))
    total = sum(runs for *_, runs in tasks)
    where = (f" on {workers} workers" + (f" (GPUs {gpu_devices})" if gpu_devices else "")) if workers > 1 else ""
    replies, done = [None] * len(tasks), 0
    slots, rows, shown = {}, [], [0.0]  # worker pid -> row, the rows, when they were last passed on

    def note(pid, doing, fraction):
        if pid not in slots:
            slots[pid] = len(rows)
            rows.append({"worker": len(rows) + 1, "doing": None, "fraction": 0.0})
        rows[slots[pid]].update(doing=doing, fraction=fraction)

    def report(status, force=False):
        if worker_status is None:
            return
        while status is not None:
            try:
                note(*status.get_nowait())
            except Exception:  # queue.Empty, from either kind of queue
                break
        if force or time.monotonic() - shown[0] >= 0.25:
            shown[0] = time.monotonic()
            worker_status([dict(row) for row in rows])

    def finished(number, reply):
        nonlocal done
        replies[number] = reply
        done += tasks[number][2]
        if progress is not None:
            kind = "stress" if style == "stress" else "scenario"
            progress(done / max(total, 1), f"{done} of {total} {kind} runs{where} ({_describe(tasks[number])})")

    if workers == 1:
        direct = None if worker_status is None else _Direct(lambda event: (note(*event), report(None)))
        _start_worker(candles_by_market, direct)
        try:
            for number, (function, args, _) in enumerate(tasks):
                if stop is not None and stop():
                    break
                finished(number, function(*args))
        finally:
            _start_worker({})
    else:
        import multiprocessing as mp
        from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
        from parallel_backtest import single_threaded_children
        single_threaded_children()
        # Plain candles: the real ones carry the market's database-backed indicator cache.
        histories = {key: [dict(candle) for candle in candles] for key, candles in candles_by_market.items()
                     if any(args[0][:2] == key for _, args, _ in tasks)}
        context = mp.get_context("spawn")
        status = context.Queue() if worker_status is not None else None
        with ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                 initializer=_start_worker, initargs=(histories, status)) as pool:
            # One task per worker at a time, the least-busy model's next first (see _next_tasks).
            queues, running, pending = {}, collections.Counter(), {}
            for number, task in enumerate(tasks):
                queues.setdefault(_model_of(task), []).append(number)

            def start_more():
                for number in _next_tasks(queues, running, workers - len(pending)):
                    function, args, _ = tasks[number]
                    pending[pool.submit(function, *args)] = number

            start_more()
            while pending:
                if stop is not None and stop():
                    for future in pending:
                        future.cancel()
                    break
                ready, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in ready:
                    number = pending.pop(future)
                    running[_model_of(tasks[number])] -= 1
                    finished(number, future.result())
                start_more()
                report(status)
        report(status, force=True)

    records, baselines, results, skipped = [], {}, [], []
    for (function, args, _), reply in zip(tasks, replies):
        if reply is None:
            continue
        if isinstance(reply, str):  # the scenario doesn't fit this market's history
            note = f"{args[1]}" + (f" #{args[4] + 1}" if args[5] > 1 else "") + f": {reply}"
            if note not in skipped:
                skipped.append(note)
            continue
        if function is _baseline_task:
            baselines[args[1]] = reply[0]
            results.append(reply[1])
        else:
            records.extend(record for record, _ in reply)
            results.extend(result for _, result in reply)
    return records, baselines, results, skipped


# --- reports ------------------------------------------------------------------------------

def summarize(records):
    """Headline numbers for one model's records."""
    if not records:
        return {"runs": 0}
    returns = np.array([r["return"] for r in records])
    worst = min(records, key=lambda r: r["return"])
    nightmares = [r for r in records if r["nightmare"]]
    return {
        "runs": len(records), "lost": sum(r["lost_money"] for r in records),
        "beat": sum(r["beat_buy_and_hold"] for r in records),
        "median": float(np.median(returns)), "worst": worst["return"], "worst_label": f"{worst['scenario']} ({worst['mode']})",
        "best": float(returns.max()), "worst_drawdown": max(r["max_drawdown"] for r in records),
        "nightmares": len(nightmares), "nightmare_lost": sum(r["lost_money"] for r in nightmares),
        "nightmare_halved": sum(r["return"] <= -0.5 for r in nightmares),
        "nightmare_beat": sum(r["beat_buy_and_hold"] for r in nightmares),
    }


def verdict(summary):
    """A plain-language verdict from a summary."""
    if not summary["runs"]:
        return "Not tested."
    lost_share = summary["lost"] / summary["runs"]
    parts = []
    if summary["worst"] <= -0.5:
        parts.append(f"Fragile: it lost {-summary['worst']:.0%} in its worst run")
    elif lost_share > 0.5:
        parts.append(f"Weak: it lost money in {lost_share:.0%} of runs")
    elif summary["worst"] > -0.2:
        parts.append(f"Robust: it never lost more than {-min(summary['worst'], 0):.0%}")
    else:
        parts.append(f"Mixed: worst run {summary['worst']:+.0%}, lost money in {lost_share:.0%} of runs")
    parts.append(f"beat buy and hold in {summary['beat']} of {summary['runs']} runs")
    if summary["nightmares"]:
        parts.append(f"in nightmares it lost money in {summary['nightmare_lost']} of {summary['nightmares']} and "
                     f"lost half or more in {summary['nightmare_halved']}")
    return "; ".join(parts) + "."


def _png(figure):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    buffer = io.BytesIO()
    FigureCanvasAgg(figure).print_png(buffer)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _bar_chart(records, title):
    from matplotlib.figure import Figure
    ordered = sorted(records, key=lambda r: r["return"])
    figure = Figure(figsize=(11, max(3, 0.22 * len(ordered) + 1.2)), dpi=90)
    ax = figure.add_subplot(111)
    y = np.arange(len(ordered))
    ax.barh(y + 0.2, [100 * r["return"] for r in ordered], height=0.4, color="#2a78d6", label="Model")
    ax.barh(y - 0.2, [100 * r["buy_and_hold"] for r in ordered], height=0.4, color="#9a9a96", label="Buy and hold")
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_yticks(y, [r["scenario"][:70] for r in ordered], fontsize=7)
    ax.set_xscale("symlog", linthresh=100)
    ax.set_xlabel("Return, % (log scale beyond +-100%)")
    ax.set_title(title, fontsize=10, loc="left")
    ax.legend(loc="lower right", fontsize=8)
    figure.tight_layout()
    return _png(figure)


def _worst_runs_chart(records, count=4):
    from matplotlib.figure import Figure
    worst = sorted(records, key=lambda r: r["return"])[:count]
    figure = Figure(figsize=(11, 3.2), dpi=90)
    axes = [figure.add_subplot(1, len(worst), i + 1) for i in range(len(worst))]
    for ax, record in zip(axes, worst):
        equity = np.array(record["equity"]) / record["starting_capital"]
        close = np.array(record["close"]) / record["close"][0]
        ax.plot(equity, color="#2a78d6", linewidth=1, label="Model value")
        ax.plot(close, color="#9a9a96", linewidth=1, label="Market")
        ax.axhline(1, color="#333333", linewidth=0.6, linestyle="--")
        ax.set_yscale("log")
        ax.set_title(f"{record['scenario'][:38]}\n({record['mode']}): {record['return']:+.0%}", fontsize=7)
        ax.tick_params(labelsize=6)
        ax.set_xticks([])
    if axes:
        axes[0].legend(fontsize=6, loc="lower left")
    figure.tight_layout()
    return _png(figure)


STYLE = """
body { font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f1f1f; background: #fcfcfb; }
h1 { font-size: 22px; } h2 { font-size: 17px; margin-top: 28px; }
table { border-collapse: collapse; font-size: 12px; margin: 8px 0; }
th, td { border-bottom: 1px solid #e3e2de; padding: 4px 8px; text-align: right; }
th { background: #f0efea; } td.l, th.l { text-align: left; }
.bad { color: #b3261e; font-weight: 600; } .good { color: #1a7a33; } .note { color: #555; font-size: 13px; }
.verdict { font-size: 15px; padding: 10px 14px; background: #f0efea; border-left: 4px solid #2a78d6; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; } .card { background: #f0efea; padding: 8px 12px; min-width: 150px; }
.card b { display: block; font-size: 18px; }
img { max-width: 100%; }
@media (prefers-color-scheme: dark) { body { background: #1d1d1b; color: #e8e7e3; }
  th, .verdict, .card { background: #2b2b28; } th, td { border-color: #3a3a36; } .note { color: #aaa; } }
"""


def _pct(value, signed=True):
    if value is None or (isinstance(value, float) and math.isinf(value)):
        return ""
    return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def _table(records):
    head = ("<tr><th class='l'>Scenario</th><th>Model</th><th>Buy &amp; hold</th><th>vs buy &amp; hold</th>"
            "<th>Max drawdown</th><th>Trades</th><th>Win rate</th><th>Fees</th><th>Taxes</th><th>Lost money</th>"
            "<th>Beat buy &amp; hold</th></tr>")
    rows = []
    for r in sorted(records, key=lambda r: r["return"]):
        rows.append(
            f"<tr><td class='l'>{html.escape(r['scenario'])}{' &#9888;' if r['nightmare'] else ''}</td>"
            f"<td class='{'bad' if r['lost_money'] else 'good'}'>{_pct(r['return'])}</td><td>{_pct(r['buy_and_hold'])}</td>"
            f"<td>{_pct(r['vs_buy_and_hold'])}</td><td>{_pct(r['max_drawdown'], False)}</td><td>{r['trades']}</td>"
            f"<td>{_pct(r['win_rate'], False)}</td><td>{r['fees']:,.0f}</td><td>{r['taxes']:,.0f}</td>"
            f"<td class='{'bad' if r['lost_money'] else ''}'>{'yes' if r['lost_money'] else 'no'}</td>"
            f"<td>{'yes' if r['beat_buy_and_hold'] else 'no'}</td></tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def write_model_report(path, model, records, baseline, profile=None, settings=""):
    summary = summarize(records)
    alone = [r for r in records if r["mode"] == "alone"]
    with_history = [r for r in records if r["mode"] == "with history"]
    description = ""
    if profile:
        strategy = profile.get("strategy", {})
        description = "".join(f"<p class='note'><b>{label}:</b> {html.escape(str(text))}</p>" for label, text in (
            ("Entry", strategy.get("entry_signal", {}).get("description", "")),
            ("Exit", strategy.get("exit_signal", {}).get("description", ""))) if text)
    cards = "".join(f"<div class='card'>{label}<b>{value}</b></div>" for label, value in (
        ("Runs", summary["runs"]),
        ("Lost money", f"{summary['lost']} of {summary['runs']}"),
        ("Beat buy and hold", f"{summary['beat']} of {summary['runs']}"),
        ("Median return", _pct(summary["median"])),
        ("Worst return", _pct(summary["worst"])),
        ("Worst drawdown", _pct(summary["worst_drawdown"], False)),
        ("Historic baseline", _pct(baseline["return"]) if baseline else "")))
    sections = []
    for title, chosen, note in (
            ("Scenarios alone", alone, "Each scenario measured from its first candle, after indicators warmed up on "
                                       "real history: how the model handles the scenario itself."),
            ("Scenarios with history", with_history, "The full real history with the scenario appended, measured "
                                                     "from the start: whether the scenario wipes out the model's "
                                                     "historic gains.")):
        if chosen:
            sections.append(f"<h2>{title}</h2><p class='note'>{note}</p>"
                            f"<img src='{_bar_chart(chosen, title + ': model against buy and hold')}'>"
                            f"{_table(chosen)}")
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>Stress test: {html.escape(model)}</title>
<style>{STYLE}</style></head><body>
<h1>Stress test: {html.escape(model)}</h1>
<p class='note'>{html.escape(baseline['product'] if baseline else '')} {html.escape(baseline['candles'] if baseline else '')} &middot;
{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC &middot; {html.escape(settings)}</p>
{description}
<p class='verdict'>{html.escape(verdict(summary))}</p>
<div class='cards'>{cards}</div>
<p class='note'>Worst run: {html.escape(summary.get('worst_label', ''))}. &#9888; marks nightmare scenarios.
Returns are after the 0.9% taker fee, slippage and tax; buy and hold pays none of them.</p>
<h2>Worst runs</h2><img src='{_worst_runs_chart(records) if records else ''}'>
{''.join(sections)}
</body></html>"""
    Path(path).write_text(page, encoding="utf-8")
    return summary


def write_reports(records, baselines, profiles=None, settings="", folder=None):
    """Write index.html, a report per model and results.csv; returns the index path."""
    folder = Path(folder or REPORT_DIR / datetime.now().strftime("%Y%m%d_%H%M%S"))
    folder.mkdir(parents=True, exist_ok=True)
    profiles = profiles or {}
    by_model = {}
    for record in records:
        by_model.setdefault(record["model"], []).append(record)
    rows = []
    for model in sorted(set(by_model) | set(baselines)):
        file = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in model) + ".html"
        summary = write_model_report(folder / file, model, by_model.get(model, []), baselines.get(model),
                                     profiles.get(model), settings)
        rows.append((model, file, summary, baselines.get(model)))
    rows.sort(key=lambda row: row[2].get("worst", -1e9), reverse=True)
    body = "".join(
        f"<tr><td class='l'><a href='{file}'>{html.escape(model)}</a></td>"
        f"<td class='l'>{html.escape(baseline['product'] if baseline else '')}</td>"
        f"<td>{_pct(baseline['return']) if baseline else ''}</td><td>{summary.get('runs', 0)}</td>"
        f"<td class='{'bad' if summary.get('lost') else ''}'>{summary.get('lost', 0)}</td>"
        f"<td>{summary.get('beat', 0)}</td><td>{_pct(summary.get('median'))}</td>"
        f"<td class='{'bad' if summary.get('worst', 0) < 0 else ''}'>{_pct(summary.get('worst'))}</td>"
        f"<td>{_pct(summary.get('worst_drawdown'), False)}</td>"
        f"<td>{summary.get('nightmare_lost', 0)} of {summary.get('nightmares', 0)}</td>"
        f"<td class='l'>{html.escape(verdict(summary))}</td></tr>"
        for model, file, summary, baseline in rows)
    index = folder / "index.html"
    index.write_text(f"""<!doctype html><html><head><meta charset='utf-8'><title>Stress test summary</title>
<style>{STYLE}</style></head><body><h1>Stress test summary</h1>
<p class='note'>{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC &middot; {html.escape(settings)}. Models are ranked
by their worst result: the most robust first. Every run's numbers are in <a href='results.csv'>results.csv</a>.</p>
<table><tr><th class='l'>Model</th><th class='l'>Currency</th><th>Historic</th><th>Runs</th><th>Lost money</th>
<th>Beat buy &amp; hold</th><th>Median</th><th>Worst</th><th>Worst drawdown</th><th>Nightmares lost</th>
<th class='l'>Verdict</th></tr>{body}</table></body></html>""", encoding="utf-8")
    columns = ["model", "product", "candles", "scenario", "mode", "path", "nightmare", "return", "buy_and_hold",
               "vs_buy_and_hold", "lost_money", "beat_buy_and_hold", "max_drawdown", "trades", "win_rate", "fees",
               "taxes", "ending_value"]
    with (folder / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(list(baselines.values()) + records)
    return index

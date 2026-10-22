"""Backtest many models at once on worker processes, without read/write collisions.

    python parallel_backtest.py config/btc_*.json --workers 4
    python parallel_backtest.py --gather          # merge results a failed run left behind

Each run splits the work into three roles, so only one of them ever writes to each file:

    candle writer (1 thread)    the only writer to data/candles.db. It downloads each
                                market's missing candles before any model on it starts,
                                then stores the slow indicator columns the workers computed.
    workers (N processes)       open candles.db read-only and backtest one model per task.
                                Each writes its model's performance row to a shard CSV of
                                its own under data/performance_shards/<run>/.
    gatherer (1 thread)         merges finished shards into data/model_performance.csv
                                as they arrive, under the log's lock, and deletes them.

Workers are processes, not threads: the backtest loop is plain Python, which threads would
run one at a time. CUDA mode adds one worker slot per visible GPU to the CPU worker capacity;
supported indicators can run on the GPU, while the trading simulation stays on CPU. Each
worker loads each market's candles once and reuses them for every model it runs on that market,
so memory grows with the worker count.

A shard the gatherer can't merge (say the CSV is open in Excel) stays on disk, and
`--gather` (or gather_shards) merges it later. The simulator window uses run_parallel in its
parallel mode (unless Workers is set to 1), and model_search.py's evaluators read the database
the same read-only way.
"""

import argparse
import json
import multiprocessing as mp
import os
import queue
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from backtest import DEFAULT_CACHE_DIR, load_candles, lookback_window, resolve_granularity, run_backtest
from candle_store import CandleStore, MarketCandles, MarketMetrics
from friction import FrictionModel
from performance_log import (
    DEFAULT_PATH as PERFORMANCE_LOG, SHARD_DIR, describe_update, gather_shards, performance_row, write_shard,
)

# CPUs kept free of workers: one for the window and the candle writer, one for the gatherer.
RESERVED_CPUS = 2
# Result keys dropped when the caller doesn't need charts or trade tables; they dominate its size.
HEAVY_KEYS = ("series", "indicators", "rules", "trades", "friction")
OFFLINE_FRICTION = {"exchange_fees": None, "exchange_fee_error": "offline", "network_fee": None,
                    "network_fee_error": None}


def cpu_count():
    """The CPUs (logical cores) this process may run on."""
    counter = getattr(os, "process_cpu_count", None)  # Python 3.13+: respects CPU affinity
    return (counter() if counter else None) or os.cpu_count() or 1


def max_workers():
    """The most worker processes a run uses: one per CPU but RESERVED_CPUS, and at least one.

    One spare CPU keeps the simulator window and the candle writer (price history downloads)
    responsive; the other runs the gatherer, which consolidates the workers' results.
    """
    return max(1, cpu_count() - RESERVED_CPUS)


# Thread pools of numpy's maths libraries; one thread each keeps every model on one thread.
SINGLE_THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def single_threaded_children():
    """Have the worker processes started from now on run their maths libraries on one thread each, so
    one model is only ever worked on by one thread. Spawned processes read these when they start;
    a value already set (by the user) is kept."""
    for name in SINGLE_THREAD_ENV:
        os.environ.setdefault(name, "1")


def parse_workers(text):
    """A worker count typed by the user: "auto" (or blank) for max_workers(), else a whole number.

    A number above max_workers() is lowered to it, so a run never takes every CPU.
    """
    text = str(text).strip().lower()
    if text in ("", "auto", "max"):
        return max_workers()
    try:
        workers = int(text)
    except ValueError:
        raise ValueError(f"Parallel workers {text!r} must be auto or a whole number such as 1 or 4") from None
    if workers < 1:
        raise ValueError("Parallel workers must be at least 1")
    return min(workers, max_workers())


def resolve_compute_device(compute_device):
    """Resolve automatic GPU selection or validate an explicitly requested CUDA backend."""
    if compute_device == "auto":
        from gpu_indicators import available_devices

        devices = available_devices()
        return ("cuda", devices) if devices else ("cpu", [])
    if compute_device == "cuda":
        from gpu_indicators import require_devices

        return "cuda", require_devices()
    if compute_device == "cpu":
        return "cpu", []
    raise ValueError(f"Unsupported compute_device {compute_device!r}; use 'auto', 'cpu' or 'cuda'")


def effective_worker_count(model_count, workers=None, gpu_slots=0):
    """CPU worker budget plus one extra worker slot per available GPU, capped by queued models."""
    cpu_workers = max(1, min(workers or max_workers(), max_workers()))
    return min(model_count, cpu_workers + gpu_slots) if model_count else 1


# --- worker processes ----------------------------------------------------------------

_worker = {}  # this process's settings, read-only store and loaded markets


def _start_worker(cache_dir, friction_inputs, start_time, wait_for_fresh_signal, shard_dir,
                  compute_device, gpu_devices):
    _worker.update(store=CandleStore(cache_dir, read_only=True), friction_inputs=friction_inputs,
                   start_time=start_time, wait=wait_for_fresh_signal, markets={},
                   shard_dir=None if shard_dir is None else Path(shard_dir),
                   compute_device=compute_device, gpu_devices=gpu_devices)


def _market(product_id, granularity, start, end):
    """The market's candles in [start, end), read once per worker, with its shared metric cache."""
    key = (product_id, granularity, start, end)
    if key not in _worker["markets"]:
        store = _worker["store"]
        cached = store.load(product_id, granularity)
        if cached is None:
            raise RuntimeError(f"{product_id} {granularity} has no cached candles")
        candles = [candle for candle in cached[0] if start <= candle["start"] < end]
        if not candles:
            raise RuntimeError(f"{product_id} {granularity} has no cached candles in the test window")
        _worker["markets"][key] = MarketCandles(
            candles, MarketMetrics(store, store.table_name(product_id, granularity)))
    return _worker["markets"][key]


def _backtest_task(index, name, profile, market, keep_heavy):
    """Backtest one model and write its performance row to this task's own shard (if logging).

    Returns the result (or the error), the shard and the metric columns this worker held
    back from the database, for the candle writer to store.
    """
    reply = {"index": index, "name": name}
    try:
        product_id, granularity, start, end = market
        result = run_backtest(_market(*market), profile, friction=FrictionModel(profile, _worker["friction_inputs"]),
                              start_time=_worker["start_time"], wait_for_fresh_signal=_worker["wait"],
                              compute_device=_worker["compute_device"],
                              device_id=(_worker["gpu_devices"][index % len(_worker["gpu_devices"])]
                                         if _worker["gpu_devices"] else 0))
        result["model_name"], result["granularity"] = name, granularity
        if _worker["shard_dir"] is not None:
            reply["shard"] = write_shard([performance_row(result, profile)],
                                         _worker["shard_dir"] / f"{index:05d}_{os.getpid()}.csv")
        if not keep_heavy:
            for key in HEAVY_KEYS:
                result.pop(key, None)
        reply["result"] = result
    except Exception as exc:  # one broken model must not stop the others
        reply["error"] = f"{type(exc).__name__}: {exc}"
    reply["metrics"] = _worker["store"].take_pending_metrics()
    return reply


# --- the coordinator's threads -------------------------------------------------------

def _download(client, cache_dir, product_id, granularity, start, end, progress):
    """Bring the market's cached candles up to date; runs on the candle writer thread."""
    if client is None:
        progress(1.0, "using cached candles (offline)")
    else:
        load_candles(client, product_id, granularity, start, end, cache_dir=cache_dir, progress=progress)


def _store_metrics(cache_dir, pending):
    """Store the metric columns a worker computed; runs on the candle writer thread."""
    try:
        CandleStore(cache_dir).apply_metrics(pending)
    except Exception:  # stored metrics only save time; a run never fails over them
        pass


class _Gatherer(threading.Thread):
    """Merges finished shards into the performance log, a batch at a time, off the coordinator."""

    def __init__(self, path, now=None):
        super().__init__(name="performance-gatherer", daemon=True)
        self.path, self.now = path, now
        self.shards = queue.Queue()
        self.outcome = {"added": [], "updated": [], "unchanged": []}
        self.left, self.error = [], None

    def run(self):
        finished = False
        while not finished:
            batch = [self.shards.get()]
            while True:
                try:
                    batch.append(self.shards.get_nowait())
                except queue.Empty:
                    break
            finished = None in batch
            batch = self.left + [shard for shard in batch if shard is not None]
            if not batch:
                continue
            try:
                merged = gather_shards(batch, self.path, self.now)
            except Exception as exc:  # e.g. the CSV is open in Excel; retried with the next batch
                self.left, self.error = batch, exc
                continue
            self.left, self.error = [], None
            for kind, names in merged.items():
                self.outcome[kind].extend(names)

    def describe(self):
        text = describe_update(self.outcome, self.path)
        if self.left:
            text += (f"; {len(self.left)} result(s) not merged ({self.error}), kept in "
                     f"{Path(self.left[0]).parent} for `python parallel_backtest.py --gather`")
        return text


def run_parallel(client, models, workers=None, start_time=None, wait_for_fresh_signal=False,
                 friction_inputs=None, cache_dir=DEFAULT_CACHE_DIR, performance_path=PERFORMANCE_LOG,
                 shard_root=None, progress=None, keep_heavy=True, now=None, compute_device="auto", memory_guard=None):
    """Backtest each (name, profile) in `models` on `workers` processes; see the module docstring.

    `workers` defaults to max_workers() and never exceeds it, nor the number of models.
    `client` downloads missing candles on the candle writer thread; with None, only cached
    candles are used. `friction_inputs` is the dict from fetch_friction_inputs (None means
    each profile's fallback rates). `start_time` and `wait_for_fresh_signal` are passed to
    every run_backtest. `progress(phase, fraction, message)`, with phase "data" or
    "simulation", may be called from any of the run's threads. Without `keep_heavy`, the
    results leave out HEAVY_KEYS. With `performance_path` None, nothing is logged. With a
    memory_guard.MemoryGuard, the results keep less chart data as memory fills, and the models not
    yet started are cancelled at the guard's budget (memory_guard.stopped then says so).

    Returns {"results": [result or None, in model order], "errors": {name: message},
    "logged": a status line for the performance log}.
    """
    progress = progress or (lambda phase, fraction, message: None)
    compute_device, gpu_devices = resolve_compute_device(compute_device)
    cpu_workers = max(1, min(workers or max_workers(), max_workers()))
    gpu_workers = min(len(gpu_devices), max(0, len(models) - cpu_workers))
    workers = effective_worker_count(len(models), cpu_workers, gpu_workers)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(os.getpid())
    shard_dir = None if performance_path is None else (
        Path(shard_root or Path(performance_path).parent / SHARD_DIR.name) / run_id)

    # Models on the same product and candle size share one download and one test window.
    markets = {}
    for index, (_, profile) in enumerate(models):
        granularity, seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        key = (profile["product_id"], granularity)
        markets.setdefault(key, {"window": lookback_window(seconds), "models": []})["models"].append(index)

    results, errors = [None] * len(models), {}
    stored = set()  # (table, column) written this run; another worker's copy holds the same values
    gatherer = _Gatherer(performance_path, now) if performance_path is not None else None
    if gatherer is not None:
        gatherer.start()
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candle-writer")
    single_threaded_children()
    pool = ProcessPoolExecutor(
        max_workers=workers, mp_context=mp.get_context("spawn"), initializer=_start_worker,
        initargs=(cache_dir, friction_inputs, start_time, wait_for_fresh_signal, shard_dir,
                  compute_device, gpu_devices))
    try:
        downloads = {}
        for number, ((product_id, granularity), market) in enumerate(markets.items()):
            prefix = f"{product_id} {granularity}" + (f" ({number + 1} of {len(markets)})" if len(markets) > 1 else "")
            report = (lambda fraction, message, number=number, prefix=prefix: progress(
                "data", (number + fraction) / len(markets), f"{prefix}: {message}"))
            downloads[writer.submit(_download, client, cache_dir, product_id, granularity,
                                    *market["window"], report)] = (product_id, granularity)

        pending, done_count = set(downloads), 0
        worker_description = f"{workers} workers"
        if gpu_workers:
            worker_description += f" ({workers - gpu_workers} CPU-capacity + {gpu_workers} GPU-added)"
        if gpu_devices:
            worker_description += f"; CUDA devices {gpu_devices}"
        progress("simulation", 0.0, f"0 of {len(models)} models done on {worker_description}")
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                if future.cancelled():  # cancelled at the memory limit
                    continue
                if future in downloads:
                    future.result()  # a failed download stops the run
                    if memory_guard is not None and memory_guard.stopped:
                        continue  # a download that was under way at the memory limit: start no more models
                    key = downloads[future]
                    market = (*key, *markets[key]["window"])
                    for index in markets[key]["models"]:
                        name, profile = models[index]
                        pending.add(pool.submit(_backtest_task, index, name, profile, market, keep_heavy))
                    continue
                reply = future.result()
                metrics = []
                for table, columns, extend in reply["metrics"]:
                    columns = {column: values for column, values in columns.items()
                               if (table, column) not in stored}
                    stored.update((table, column) for column in columns)
                    if columns:
                        metrics.append((table, columns, extend))
                if metrics:
                    writer.submit(_store_metrics, cache_dir, metrics)
                if "error" in reply:
                    errors[reply["name"]] = reply["error"]
                else:
                    results[reply["index"]] = reply["result"]
                    if gatherer is not None:
                        gatherer.shards.put(reply["shard"])
                    if memory_guard is not None and not memory_guard.admit(
                            [reply["result"]], (result for result in results if result is not None)):
                        for other in pending:  # the models not started yet; running ones finish
                            other.cancel()
                done_count += 1
                progress("simulation", done_count / len(models),
                         f"{done_count} of {len(models)} models done ({reply['name']}"
                         + (" failed)" if "error" in reply else ")"))
    finally:
        pool.shutdown(cancel_futures=True)
        writer.shutdown()  # the metric writes still queued finish first
        if gatherer is not None:
            gatherer.shards.put(None)
            gatherer.join()
    if gatherer is None:
        logged = "performance log not updated"
    else:
        logged = gatherer.describe()
        if not gatherer.left:
            try:
                shard_dir.rmdir()  # empty once every shard was merged
            except OSError:
                pass
    return {"results": results, "errors": errors, "logged": logged}


# --- command line --------------------------------------------------------------------

def market_inputs(profiles, offline=False):
    """(client, friction inputs) for running `profiles`: a Coinbase client and the live fees, or with
    `offline` no client (cached candles only) and each profile's fallback fees."""
    if offline:
        return None, OFFLINE_FRICTION
    from engine import QuantitativeTradingEngine
    from friction import fetch_friction_inputs
    client = QuantitativeTradingEngine(base_directory=".", node_is_primary=False).create_coinbase_client()
    return client, fetch_friction_inputs(client, profiles)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("profiles", nargs="*", help="model profile JSON files to backtest")
    parser.add_argument("--workers", type=parse_workers, default="auto",
                        help=f"CPU-capacity worker processes: auto (the default) or a number, at most "
                             f"{max_workers()} here (one per CPU but {RESERVED_CPUS}); CUDA adds a slot per GPU")
    parser.add_argument("--start", help="start testing from this UTC date (YYYY-MM-DD); indicators still warm up")
    parser.add_argument("--fresh-signal", action="store_true", help="wait for a fresh entry signal at the start")
    parser.add_argument("--offline", action="store_true",
                        help="use cached candles only, and each profile's fallback fee")
    parser.add_argument("--compute-device", choices=("auto", "cpu", "cuda"), default="auto",
                        help="auto (default) uses visible CUDA GPUs; cpu or cuda forces the device choice")
    parser.add_argument("--no-log", action="store_true", help="leave the performance log alone")
    parser.add_argument("--gather", action="store_true",
                        help="merge result shards left by earlier runs into the performance log, then exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.gather:
        if not SHARD_DIR.exists():
            print("No result shards to gather")
            return
        print(describe_update(gather_shards(SHARD_DIR), PERFORMANCE_LOG))
        for folder in sorted(SHARD_DIR.iterdir(), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        return
    if not args.profiles:
        sys.exit("Name at least one profile, e.g. python parallel_backtest.py config/*.json")

    from simulator import describe_profile, model_names, parse_start_date

    paths = [Path(path) for path in args.profiles]
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    models = list(zip(model_names(paths), profiles))
    problems = [f"{name}: {report.splitlines()[1][2:]}" for name, profile in models
                for valid, report in [describe_profile(profile)] if not valid]
    if problems:
        sys.exit("Fix these profiles first (the simulator's Load Profile lists every problem):\n" + "\n".join(problems))

    client, friction_inputs = market_inputs(profiles, args.offline)

    last = {}

    def progress(phase, fraction, message):
        # Printed at most every 2 seconds per phase, and always at the end.
        if fraction >= 1 or time.monotonic() - last.get(phase, 0) > 2:
            last[phase] = time.monotonic()
            print(f"  {phase:10s} {fraction:4.0%}  {message}", flush=True)

    started = time.monotonic()
    resolved_device, devices = resolve_compute_device(args.compute_device)
    cpu_slots = min(args.workers, max_workers())
    gpu_slots = min(len(devices), max(0, len(models) - cpu_slots))
    worker_count = effective_worker_count(len(models), cpu_slots, gpu_slots)
    extra = f" ({cpu_slots} CPU-capacity + {gpu_slots} GPU-added)" if gpu_slots else ""
    device_description = f" using CUDA devices {devices}" if devices else ""
    print(f"Backtesting {len(models)} model(s) on {worker_count} worker(s){extra}{device_description}", flush=True)
    run = run_parallel(client, models, workers=args.workers, start_time=parse_start_date(args.start or ""),
                       wait_for_fresh_signal=args.fresh_signal, friction_inputs=friction_inputs,
                       performance_path=None if args.no_log else PERFORMANCE_LOG, progress=progress,
                       keep_heavy=False, compute_device=resolved_device)
    print(f"\nFinished in {time.monotonic() - started:,.0f}s\n")
    print(f"{'model':40s} {'return':>12s} {'buy & hold':>12s} {'max dd':>8s} {'trades':>7s}")
    for (name, _), result in zip(models, run["results"]):
        if result is None:
            print(f"{name:40s} FAILED: {run['errors'][name]}")
        else:
            print(f"{name:40s} {result['total_return']:+12.2%} {result['buy_and_hold_return']:+12.2%} "
                  f"{result['max_drawdown']:8.1%} {result['trade_count']:7,}")
    print(f"\n{run['logged']}")
    if run["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

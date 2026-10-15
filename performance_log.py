"""A CSV record of every model's latest test results, kept by the simulator.

Each model has one row, keyed by its name (the profile's file name). A run of a model
that isn't in the file adds its row; a run whose results or run conditions differ from
the stored row replaces it; an identical run leaves the file untouched.

Several runs can record results at once. Every update holds a lock file next to the CSV
while it reads, merges and replaces it, so no run's rows overwrite another's. Parallel
runs (see parallel_backtest.py) go one step further: each worker writes its rows to a shard
CSV of its own (write_shard), and one gatherer merges finished shards into the shared file
(gather_shards), deleting each shard once its rows are in.
"""

import contextlib
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "data" / "model_performance.csv"
SHARD_DIR = DEFAULT_PATH.parent / "performance_shards"
# A lock file older than this is left over from a crashed run, and is taken over.
STALE_LOCK_SECONDS = 120
LOCK_TIMEOUT_SECONDS = 60

# Compared to decide whether a run changed anything; the timestamps below are not.
RECORDED_COLUMNS = [
    "model", "product", "candles", "profile_fingerprint", "test_start", "test_end", "requested_start",
    "wait_for_fresh_signal", "commission_rate_pct", "starting_capital", "ending_value", "growth_multiple",
    "return_pct", "first_buy", "buy_and_hold_start", "buy_and_hold_multiple", "buy_and_hold_return_pct",
    "vs_buy_and_hold_pct", "max_drawdown_pct", "worst_in_window_pct", "drawdown_window", "trades",
    "win_rate_pct", "fees", "taxes", "blocked_signals", "flash_buys",
]
COLUMNS = RECORDED_COLUMNS + ["first_recorded_utc", "last_changed_utc"]


def profile_fingerprint(profile):
    """A short hash of the profile, so a row shows whether the model's definition changed."""
    text = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _time(timestamp):
    return "" if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _number(value):
    return f"{value:.10g}"  # stable text, so an unchanged result compares equal


def performance_row(result, profile):
    """The CSV row (all text) for one backtest result of `profile`."""
    growth = 1 + result["total_return"]
    held = 1 + result["buy_and_hold_return"]
    return {
        "model": result.get("model_name", result["product_id"]),
        "product": result["product_id"],
        "candles": result.get("granularity", profile.get("period_granularity", "")),
        "profile_fingerprint": profile_fingerprint(profile),
        "test_start": _time(result["period_start"]),
        "test_end": _time(result["period_end"]),
        "requested_start": _time(result.get("requested_start")),
        "wait_for_fresh_signal": "yes" if result.get("waited_for_fresh_signal") else "no",
        "commission_rate_pct": _number(result["commission_rate"] * 100),
        "starting_capital": _number(result["starting_capital"]),
        "ending_value": _number(result["ending_equity"]),
        "growth_multiple": _number(growth),
        "return_pct": _number(result["total_return"] * 100),
        "first_buy": _time(result.get("first_entry")),
        "buy_and_hold_start": _time(result.get("buy_and_hold_start", result["period_start"])),
        "buy_and_hold_multiple": _number(held),
        "buy_and_hold_return_pct": _number(result["buy_and_hold_return"] * 100),
        "vs_buy_and_hold_pct": _number((growth / held - 1) * 100) if held > 0 else "",
        "max_drawdown_pct": _number(result["max_drawdown"] * 100),
        "worst_in_window_pct": _number(result["max_window_drawdown"] * 100),
        "drawdown_window": str(result["drawdown_window"]),
        "trades": str(result["trade_count"]),
        "win_rate_pct": _number(result["win_rate"] * 100),
        "fees": _number(result["fees_paid"]),
        "taxes": _number(result["taxes_paid"]),
        "blocked_signals": str(result["screened_signals"]),
        "flash_buys": str(len(result["flash_crash_buys"])),
    }


@contextlib.contextmanager
def _locked(path):
    """Hold `path`.lock, so one update at a time, in any thread or process, reads and replaces `path`."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    timeout = LOCK_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue  # released between the two calls
            if time.monotonic() > deadline:
                raise TimeoutError(f"{lock} has been held for over {timeout}s; delete it if no run is recording")
            time.sleep(0.05)
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        lock.unlink(missing_ok=True)


def update_performance_log(entries, path=DEFAULT_PATH, now=None):
    """Record each (result, profile) pair in the CSV at `path`.

    Returns the model names {"added": [...], "updated": [...], "unchanged": [...]}. The
    file is rewritten only when something was added or updated, through a temporary file
    so a failed write never leaves it half written.
    """
    return merge_rows([performance_row(result, profile) for result, profile in entries], path, now)


def merge_rows(new_rows, path=DEFAULT_PATH, now=None):
    """Record performance rows (from performance_row) in the CSV at `path`, as update_performance_log does."""
    path = Path(path)
    with _locked(path):
        return _merge_rows(new_rows, path, now)


def _merge_rows(new_rows, path, now):
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            rows = {row["model"]: row for row in csv.DictReader(handle)}
    outcome = {"added": [], "updated": [], "unchanged": []}
    for row in new_rows:
        row = {column: row.get(column, "") for column in RECORDED_COLUMNS}
        old = rows.get(row["model"])
        if old is not None and all(old.get(column, "") == row[column] for column in RECORDED_COLUMNS):
            outcome["unchanged"].append(row["model"])
            continue
        row["first_recorded_utc"] = (old or {}).get("first_recorded_utc") or stamp
        row["last_changed_utc"] = stamp
        outcome["updated" if old is not None else "added"].append(row["model"])
        rows[row["model"]] = row
    if outcome["added"] or outcome["updated"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            for model in sorted(rows):
                writer.writerow({column: rows[model].get(column, "") for column in COLUMNS})
        try:
            os.replace(temporary, path)
        except OSError:  # e.g. the CSV is open in Excel, which locks it on Windows
            temporary.unlink(missing_ok=True)
            raise
    return outcome


def write_shard(rows, shard_path):
    """Write performance rows (from performance_row) to their own shard CSV, for gather_shards.

    The shard appears complete or not at all: it is written under a temporary name first.
    """
    shard_path = Path(shard_path)
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = shard_path.with_name(shard_path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECORDED_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in RECORDED_COLUMNS})
    os.replace(temporary, shard_path)
    return shard_path


def gather_shards(shards, path=DEFAULT_PATH, now=None):
    """Merge shard CSVs into the CSV at `path`, then delete them; returns the outcome as merge_rows does.

    `shards` is a list of shard files, or a folder whose shards (in any subfolder) are all
    gathered. Shards later in the list win when two hold the same model. If the merge
    fails, for example because the CSV is open in Excel, every shard is kept for a later
    gather. A shard another gatherer took first is skipped.
    """
    if isinstance(shards, (str, Path)) and Path(shards).is_dir():
        shards = sorted(Path(shards).rglob("*.csv"), key=lambda shard: shard.stat().st_mtime)
    path = Path(path)
    with _locked(path):
        rows, read = [], []
        for shard in map(Path, shards):
            try:
                with shard.open(newline="", encoding="utf-8") as handle:
                    rows.extend(csv.DictReader(handle))
            except FileNotFoundError:
                continue
            read.append(shard)
        outcome = _merge_rows(rows, path, now)
        for shard in read:
            shard.unlink(missing_ok=True)
    return outcome


def describe_update(outcome, path=DEFAULT_PATH):
    """One status-line summary of an update, such as "model_performance.csv: 2 added, 1 updated"."""
    parts = [f"{len(outcome[kind])} {kind}" for kind in ("added", "updated", "unchanged") if outcome[kind]]
    return f"{Path(path).name}: {', '.join(parts) or 'nothing to record'}"

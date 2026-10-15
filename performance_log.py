"""A CSV record of every model's latest test results, kept by the simulator.

Each model has one row, keyed by its name (the profile's file name). A run of a model
that isn't in the file adds its row; a run whose results or run conditions differ from
the stored row replaces it; an identical run leaves the file untouched.
"""

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "data" / "model_performance.csv"

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


def update_performance_log(entries, path=DEFAULT_PATH, now=None):
    """Record each (result, profile) pair in the CSV at `path`.

    Returns the model names {"added": [...], "updated": [...], "unchanged": [...]}. The
    file is rewritten only when something was added or updated, through a temporary file
    so a failed write never leaves it half written.
    """
    path = Path(path)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            rows = {row["model"]: row for row in csv.DictReader(handle)}
    outcome = {"added": [], "updated": [], "unchanged": []}
    for result, profile in entries:
        row = performance_row(result, profile)
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


def describe_update(outcome, path=DEFAULT_PATH):
    """One status-line summary of an update, such as "model_performance.csv: 2 added, 1 updated"."""
    parts = [f"{len(outcome[kind])} {kind}" for kind in ("added", "updated", "unchanged") if outcome[kind]]
    return f"{Path(path).name}: {', '.join(parts) or 'nothing to record'}"

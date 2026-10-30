"""Recovering the live trader after a power cut, a crash or a move to another machine.

What the trader needs to carry on is small, and all of it is in a *recovery bundle* (a zip file):

    manifest.json               when and where it was made, and what it holds
    ledger.db                   the trading ledger: every deployment's money, stored profile, orders and trades
    live_config/live_trading.json, live_config/HALT
                                the settings, and the HALT switch if it was on
    tax/tax_profile.json        your tax situation (without it, tax reserves fall back to the example's)
    profiles/<path>             the profile files the settings name, at their paths under the program folder
    data/candles.db             only with --with-candles: the price history, so a new machine needn't download it

It never holds .env: your API keys stay on the machines you put them on.

The trader writes a bundle to the data folder's backups/ (see data_home.py) every settings.backup_hours (24 by
default), at every start and at every clean stop, keeping the newest settings.backup_keep.
`python live_trader.py --backup` writes one now, and `--restore <bundle>` puts one in place on a new (or
repaired) machine. The ledger is copied with SQLite's backup API, so a bundle is consistent even while the
trader is running.

export_reports() writes the ledger's records as CSV files to the data folder's reports/, so your trading
history stays readable in any spreadsheet even without the program.

Starting from a restored or surviving ledger needs nothing more: the trader replays each model from when it
was deployed, so it reaches the same decisions it would have made had it never stopped (see live_trader.py).
"""

import csv
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_VERSION = 1
BACKUP_DIR = "backups"
BUNDLE_PREFIX = "live_trader_backup_"
LEDGER_ENTRY = "ledger.db"
CANDLES_ENTRY = "data/candles.db"


class RecoveryError(RuntimeError):
    pass


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d_%H%M%S")


def _when(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# --- the ledger's health -----------------------------------------------------------------------------

def integrity_problem(path):
    """None when the SQLite file at `path` is sound (or doesn't exist yet), else what is wrong with it."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    results = [row[0] for row in rows]
    return None if results == ["ok"] else "; ".join(results[:5])


def snapshot_ledger(source, destination):
    """Copy the SQLite database at `source` to `destination`, consistently, even while it is being written."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(str(source))
    writer = sqlite3.connect(str(destination))
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


# --- writing a bundle --------------------------------------------------------------------------------

def _profile_files(config, base_dir):
    """The profile files the settings name, as (path on disk, path in the bundle), for those inside base_dir."""
    files = []
    base_dir = Path(base_dir).resolve()
    for item in (config or {}).get("deployments", []):
        reference = item.get("profile", "")
        if reference.lower().startswith("best:"):
            continue  # the ledger holds the deployed copy; a currency's best is chosen on its own machine
        path = Path(reference)
        path = (path if path.is_absolute() else base_dir / path).resolve()
        try:
            relative = path.relative_to(base_dir)
        except ValueError:
            continue
        if path.is_file():
            files.append((path, f"profiles/{relative.as_posix()}"))
    return files


def write_bundle(destination, base_dir, ledger_path, config_path, now, include_candles=False, cache_dir=None,
                 tax_profile=None, mode=None):
    """Write a recovery bundle to `destination` (a .zip path); returns its manifest. `base_dir` is the program
    folder that relative profile paths in the settings start from; `tax_profile` the tax profile to include."""
    base_dir, destination = Path(base_dir), Path(destination)
    ledger_path, config_path = Path(ledger_path), Path(config_path)
    if not ledger_path.exists():
        raise RecoveryError(f"There is no trading ledger at {ledger_path} to back up")
    problem = integrity_problem(ledger_path)
    if problem:
        raise RecoveryError(f"The ledger at {ledger_path} is damaged ({problem}); restore an earlier backup "
                            f"instead of backing this one up")
    tax_profile = Path(tax_profile) if tax_profile else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    with tempfile.TemporaryDirectory() as work:
        ledger_copy = Path(work) / LEDGER_ENTRY
        snapshot_ledger(ledger_path, ledger_copy)
        # closing(): a sqlite3 connection's own `with` commits but leaves the file open, and Windows can't
        # delete an open file when the temporary folder is cleaned up.
        with closing(sqlite3.connect(str(ledger_copy))) as connection:
            deployments = [dict(zip(("id", "product_id", "mode", "status"), row)) for row in connection.execute(
                "SELECT id, product_id, mode, status FROM deployments ORDER BY id")]
        contents = [LEDGER_ENTRY]
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(ledger_copy, LEDGER_ENTRY)
            config = None
            if config_path.exists():
                bundle.write(config_path, "live_config/live_trading.json")
                contents.append("live_config/live_trading.json")
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                except ValueError:
                    config = None
                halt = config_path.parent / "HALT"
                if halt.exists():
                    bundle.write(halt, "live_config/HALT")
                    contents.append("live_config/HALT")
            if tax_profile is not None and tax_profile.exists():
                bundle.write(tax_profile, "tax/tax_profile.json")
                contents.append("tax/tax_profile.json")
            for path, name in _profile_files(config, base_dir):
                bundle.write(path, name)
                contents.append(name)
            if include_candles:
                candles = Path(cache_dir) / "candles.db"
                if candles.exists():
                    candle_copy = Path(work) / "candles.db"
                    snapshot_ledger(candles, candle_copy)
                    bundle.write(candle_copy, CANDLES_ENTRY)
                    contents.append(CANDLES_ENTRY)
            modes = sorted({d["mode"] for d in deployments}) or [mode or "paper"]
            if mode is not None and modes != [mode]:
                raise RecoveryError(f"{ledger_path} holds {', '.join(modes)} deployments, not only {mode}")
            manifest = {"bundle_version": BUNDLE_VERSION, "mode": modes[0] if len(modes) == 1 else "mixed",
                        "created": int(now), "created_utc": _when(now),
                        "host": socket.gethostname(), "base_dir": str(base_dir.resolve()),
                        "deployments": deployments, "contents": contents}
            bundle.writestr("manifest.json", json.dumps(manifest, indent=1))
    os.replace(temporary, destination)
    return manifest


def automatic_backup(folder, base_dir, ledger_path, config_path, now, keep=14, tax_profile=None, mode=None):
    """Write a dated bundle to `folder` (the data folder's backups/) and delete all but the newest `keep`.
    Returns its path."""
    folder = Path(folder)
    path = folder / f"{BUNDLE_PREFIX}{_stamp(now)}.zip"
    write_bundle(path, base_dir, ledger_path, config_path, now, tax_profile=tax_profile, mode=mode)
    prune(folder, keep)
    return path


def bundles(folder):
    """The automatic bundles in `folder`, newest first."""
    return sorted(Path(folder).glob(f"{BUNDLE_PREFIX}*.zip"), reverse=True)


def prune(folder, keep):
    for old in bundles(folder)[max(int(keep), 1):]:
        try:
            old.unlink()
        except OSError:
            pass


def latest_bundle(folder):
    found = bundles(folder)
    return found[0] if found else None


# --- restoring a bundle ------------------------------------------------------------------------------

def read_manifest(bundle_path):
    try:
        with zipfile.ZipFile(bundle_path) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            names = set(bundle.namelist())
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise RecoveryError(f"{bundle_path} is not a live trader recovery bundle: {exc}") from None
    if LEDGER_ENTRY not in names:
        raise RecoveryError(f"{bundle_path} holds no ledger")
    if manifest.get("bundle_version", 0) > BUNDLE_VERSION:
        raise RecoveryError(f"{bundle_path} was written by a newer version of the trader; update this copy first")
    return manifest


def bundle_mode(bundle_path):
    """Whose records a bundle holds: "live" or "paper". A bundle from before live and paper were kept apart is
    judged by its deployments; one mixing both can't be restored into either."""
    manifest = read_manifest(bundle_path)
    mode = manifest.get("mode")
    if mode is None:
        modes = {d.get("mode") for d in manifest.get("deployments", [])} or {"paper"}
        mode = modes.pop() if len(modes) == 1 else "mixed"
    if mode not in ("live", "paper"):
        raise RecoveryError(f"{bundle_path} mixes live and paper deployments, which are kept apart now; it can't "
                            f"be restored into either")
    return mode


def restore_bundle(bundle_path, paths, now, ledger_path=None, config_path=None):
    """Put a bundle's files in place in the data folder `paths` (a data_home.DataPaths). Every file it would
    replace is first moved to backups/replaced_<time>/, so nothing is lost. Returns (manifest, [restored paths],
    the replaced folder or None)."""
    manifest = read_manifest(bundle_path)
    ledger_path, config_path = Path(ledger_path or paths.ledger), Path(config_path or paths.settings)
    targets = {LEDGER_ENTRY: ledger_path, "live_config/live_trading.json": config_path,
               "live_config/HALT": config_path.parent / "HALT", "tax/tax_profile.json": paths.tax_profile,
               CANDLES_ENTRY: paths.cache / "candles.db"}
    replaced = paths.backups / f"replaced_{_stamp(now)}"
    restored = []
    with zipfile.ZipFile(bundle_path) as bundle, tempfile.TemporaryDirectory() as work:
        bundle.extract(LEDGER_ENTRY, work)
        problem = integrity_problem(Path(work) / LEDGER_ENTRY)
        if problem:
            raise RecoveryError(f"The ledger in {bundle_path} is damaged ({problem}); try an older bundle")
        for name in bundle.namelist():
            if name == "manifest.json":
                continue
            if name.startswith("profiles/"):
                target = paths.profiles / name[len("profiles/"):]  # for reference: the ledger holds what trades
            elif name in targets:
                target = targets[name]
            else:
                continue
            if ".." in Path(name).parts:
                raise RecoveryError(f"{bundle_path} has an unsafe path: {name}")
            if target.exists():
                aside = replaced / name
                aside.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(aside))
            if name == LEDGER_ENTRY:  # a ledger's WAL files belong to the ledger they came with
                for suffix in ("-wal", "-shm"):
                    stale = Path(str(target) + suffix)
                    if stale.exists():
                        aside = replaced / (LEDGER_ENTRY + suffix)
                        aside.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(stale), str(aside))
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(name) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            restored.append(target)
    return manifest, restored, replaced if replaced.exists() else None


# --- sessions and outages ----------------------------------------------------------------------------

def describe_seconds(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


# --- readable reports ---------------------------------------------------------------------------------------

REPORTS = {
    "trades.csv": "SELECT deployment_id, product_id, mode, datetime(entry_time, 'unixepoch') AS entry_utc, "
                  "datetime(exit_time, 'unixepoch') AS exit_utc, entry_price, exit_price, units, cost, proceeds, "
                  "fees, pnl, entry_reason, exit_reason FROM trades ORDER BY exit_time, id",
    "orders.csv": "SELECT datetime(created_at, 'unixepoch') AS placed_utc, deployment_id, product_id, mode, side, "
                  "reason, order_style, status, requested_quote, requested_base, filled_base, filled_value, fees, "
                  "average_price, exchange_order_id, client_order_id, error FROM orders ORDER BY id",
    "deployments.csv": "SELECT id, product_id, mode, status, capital, cash, units, cost_basis, fees_paid, "
                       "datetime(started_at, 'unixepoch') AS deployed_utc, datetime(entry_time, 'unixepoch') AS "
                       "position_since_utc, entry_price, entry_reason, review_note, profile_path, "
                       "datetime(updated_at, 'unixepoch') AS updated_utc FROM deployments ORDER BY id",
    "value_history.csv": "SELECT datetime(time, 'unixepoch') AS time_utc, deployment_id, price, equity AS "
                         "account_value, replay_equity AS model_replay_value FROM equity ORDER BY time, deployment_id",
    "events.csv": "SELECT datetime(time, 'unixepoch') AS time_utc, level, deployment_id, message FROM events "
                  "ORDER BY id",
}


def export_reports(ledger_path, folder):
    """Write the ledger's records as CSV files in `folder`. A file open in a spreadsheet (locked, on Windows) is
    skipped this time and written at the next export. Returns the names that couldn't be written."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    skipped = []
    with closing(sqlite3.connect(f"file:{Path(ledger_path).as_posix()}?mode=ro", uri=True)) as connection:
        for name, query in REPORTS.items():
            cursor = connection.execute(query)
            temporary = folder / (name + ".tmp")
            with open(temporary, "w", newline="", encoding="utf-8-sig") as handle:  # BOM: Excel reads it as UTF-8
                writer = csv.writer(handle)
                writer.writerow([column[0] for column in cursor.description])
                writer.writerows(cursor)
            try:
                os.replace(temporary, folder / name)
            except PermissionError:
                temporary.unlink(missing_ok=True)
                skipped.append(name)
    return skipped

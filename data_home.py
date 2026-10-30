"""Where the live trader keeps your data: one folder outside the program, so removing, updating or reinstalling
the program never touches it.

The data folder holds everything the trader writes and everything it needs to carry on. Live and paper trading
keep their records apart, in live/ and paper/, so paper trades can never be mistaken for real money:

    live/                           real trading (an API key that can trade):
        ledger/trading_ledger.db        the trading ledger: every deployment's money, stored profile, orders, trades
        reports/                        the same records as plain CSV files, readable without the program
        backups/                        recovery bundles (see recovery.py)
        profiles/                       profile files restored from a bundle, for reference
    paper/                          paper trading (a view-only key, or --paper): the same, kept apart
    live_config/live_trading.json   your settings (deployments, safeguards), and the HALT switch beside them
    tax/tax_profile.json            your tax situation
    logs/                           the trader's and the supervisor's logs
    cache/candles.db                the trader's price history (it can always be downloaded again)
    run/                            the running trader's lock and process records
    live_trader.env                 the live trader's own configuration: its API key, portal and paper switch
                                    (never the simulator's .env; see live_config/.example/live_trader.env.example)

Which folder: --data-dir, else the LIVE_TRADER_DATA_DIR environment variable, else the folder chosen with
--set-data-dir (remembered in a small file in your user settings, also outside the program), else the
standard place for an application's data on this system:

    Windows   %LOCALAPPDATA%\\ConfigurableCryptoTradingBot\\LiveTrader   (not synced by OneDrive: a database must
                                                                        never be synced while it is open)
    macOS     ~/Library/Application Support/ConfigurableCryptoTradingBot/LiveTrader
    Linux     $XDG_DATA_HOME/configurable-crypto-trading-bot/live-trader   (~/.local/share/... by default)
"""

import json
import os
import shutil
import sys
from pathlib import Path

ENV_VAR = "LIVE_TRADER_DATA_DIR"
WINDOWS_APP, UNIX_APP = "ConfigurableCryptoTradingBot", "configurable-crypto-trading-bot"
README_NAME = "README.txt"


def _user_folder(kind):
    """The OS's per-user folder for application data ("data") or settings ("config")."""
    home = Path.home()
    if os.name == "nt":
        variable = "LOCALAPPDATA" if kind == "data" else "APPDATA"
        root = Path(os.environ.get(variable) or home / "AppData" / ("Local" if kind == "data" else "Roaming"))
        return root / WINDOWS_APP
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / WINDOWS_APP
    variable, fallback = ("XDG_DATA_HOME", home / ".local" / "share") if kind == "data" else \
        ("XDG_CONFIG_HOME", home / ".config")
    return Path(os.environ.get(variable) or fallback) / UNIX_APP


def default_data_dir():
    return _user_folder("data") / ("LiveTrader" if os.name == "nt" or sys.platform == "darwin" else "live-trader")


# Remembers a folder chosen with --set-data-dir. It sits in the user's settings folder, outside the program.
POINTER_FILE = _user_folder("config") / "data_location.json"


def chosen_data_dir():
    try:
        return Path(json.loads(POINTER_FILE.read_text(encoding="utf-8"))["data_dir"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resolve_data_dir(explicit=None):
    """(the data folder, how it was chosen)."""
    if explicit:
        return Path(explicit).expanduser().resolve(), "--data-dir"
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR]).expanduser().resolve(), f"the {ENV_VAR} environment variable"
    chosen = chosen_data_dir()
    if chosen:
        return chosen, f"chosen with --set-data-dir (remembered in {POINTER_FILE})"
    return default_data_dir(), "the standard place for application data on this system"


MODES = ("live", "paper")


class DataPaths:
    """Every file and folder in a data folder. With a `mode` ("live" or "paper"), the trading records (ledger,
    reports, backups, profiles) are that mode's own, under <root>/<mode>/; everything else is shared."""

    def __init__(self, root, mode=None):
        if mode not in (None, *MODES):
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        self.root, self.mode = Path(root), mode
        self.settings = self.root / "live_config" / "live_trading.json"
        self.halt = self.root / "live_config" / "HALT"
        self.tax_profile = self.root / "tax" / "tax_profile.json"
        self.logs = self.root / "logs"
        self.log_file = self.logs / "live_trader.log"
        self.supervisor_log = self.logs / "supervisor.log"
        self.cache = self.root / "cache"
        self.run = self.root / "run"
        # The live trader's own configuration (API key, portal, FORCE_PAPER_TRADING). Never the simulator's .env.
        self.env = self.root / "live_trader.env"

    def create(self):
        """Make the folder (and its README, so anyone finding it knows what it is and that it is safe to keep)."""
        self.root.mkdir(parents=True, exist_ok=True)
        readme = self.root / README_NAME
        if not readme.exists():
            readme.write_text(README_TEXT, encoding="utf-8")
        return self

    # A mode's own records. Without a mode there are none: asking is a mistake that must not guess.
    @property
    def area(self):
        if self.mode is None:
            raise ValueError("Trading records belong to live or paper trading: choose one (DataPaths.for_mode)")
        return self.root / self.mode

    ledger = property(lambda self: self.area / "ledger" / "trading_ledger.db")
    reports = property(lambda self: self.area / "reports")
    backups = property(lambda self: self.area / "backups")
    profiles = property(lambda self: self.area / "profiles")

    def for_mode(self, mode):
        return DataPaths(self.root, mode)

    def modes_with_records(self):
        """The modes ("live", "paper") whose ledger exists here."""
        return [mode for mode in MODES if self.for_mode(mode).ledger.exists()]

    def has_data(self):
        return self.settings.exists() or bool(self.modes_with_records()) or (self.root / "ledger").exists()


def remember(folder):
    """Make `folder` the data folder for every later command (--set-data-dir)."""
    POINTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    POINTER_FILE.write_text(json.dumps({"data_dir": str(Path(folder).resolve())}, indent=1), encoding="utf-8")


def move_contents(source, destination):
    """Move everything in data folder `source` into `destination` (which must hold no trading data); the list
    of names moved."""
    source, destination = Path(source), Path(destination)
    if DataPaths(destination).has_data():
        raise ValueError(f"{destination} already holds trading data; it can't be merged with {source}")
    destination.mkdir(parents=True, exist_ok=True)
    moved = []
    for item in sorted(source.iterdir()) if source.exists() else []:
        if item.name == "run":
            continue  # a stopped trader's lock and records belong to the old place
        target = destination / item.name
        if target.exists():
            if item.name == README_NAME:
                item.unlink()
                continue
            raise ValueError(f"{target} already exists")
        shutil.move(str(item), str(target))
        moved.append(item.name)
    return moved


def ledger_modes(path):
    """The modes of the deployments in the ledger at `path` (an empty set when it has none)."""
    import sqlite3
    from contextlib import closing
    try:
        with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as connection:
            return {row[0] for row in connection.execute("SELECT DISTINCT mode FROM deployments")}
    except sqlite3.Error:
        return set()


def adopt_program_folder_data(paths, program_dir):
    """Move live trading data an earlier version wrote into the program folder, or into the top of the data folder
    (before live and paper kept separate records), into its mode's area, once: the ledger, settings and HALT
    switch, backups and logs. A ledger mixing live and paper deployments is left where it is: no area may hold
    both. Returns what was moved ([] when nothing was)."""
    program_dir = Path(program_dir)
    moved = []
    for old_root in (paths.root, program_dir):
        old_ledger = old_root / "ledger" / "trading_ledger.db"
        if not old_ledger.exists() or paths.modes_with_records():
            continue
        modes = ledger_modes(old_ledger)
        if len(modes) > 1:
            raise ValueError(f"{old_ledger} holds both live and paper deployments; it can't be moved into one "
                             f"area. Move its deployments out by hand, or leave it where it is.")
        area = paths.for_mode(modes.pop() if modes else "paper")
        moves = [(old_ledger, area.ledger)]
        moves += [(Path(str(old_ledger) + suffix), Path(str(area.ledger) + suffix)) for suffix in ("-wal", "-shm")]
        moves += [(old_root / "backups", area.backups), (old_root / "reports", area.reports),
                  (old_root / "profiles", area.profiles)]
        moved += _move_all(moves)
    old_settings = program_dir / "live_config" / "live_trading.json"
    if not paths.settings.exists() and old_settings.exists():
        moved += _move_all([(old_settings, paths.settings), (program_dir / "live_config" / "HALT", paths.halt)])
    if (program_dir / "logs" / "live_trader.log").exists():
        moved += _move_all([(item, paths.logs / item.name) for item in sorted((program_dir / "logs").glob("*"))])
    return moved


def _move_all(moves):
    moved = []
    for source, target in moves:
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append(str(target))
        elif source.is_dir() and target.is_dir():  # merge a folder's files into one that already exists
            moved += _move_all([(item, target / item.name) for item in sorted(source.iterdir())])
    return moved


README_TEXT = """Live trader data folder
=======================

This folder holds your trading data: the trading ledgers, your settings, backups, logs and readable reports.
It lives outside the trading program on purpose, so removing, updating or reinstalling the program never
deletes it. Keep it.

Real trading and paper trading keep separate records, so paper trades can never be mistaken for real money:

  live/             REAL MONEY: the trader used an API key that can trade
  paper/            paper trading: the trader used a view-only key (or was told --paper)

Each of them holds:
  reports/          your trades, orders, deployments, value history and events as CSV files (open them in any
                    spreadsheet; they are rewritten as the trader runs)
  ledger/           the trading ledger (an SQLite database): the trader's own record
  backups/          recovery bundles: copy the newest one somewhere else now and then
  profiles/         profile files restored from a bundle, for reference

Shared by both:
  live_config/      your settings (live_trading.json), and the HALT switch when you create it
  tax/              your tax profile
  logs/             what the trader did, and when
  cache/            price history (safe to delete: it is downloaded again)
  run/              the running trader's lock and process records (safe to ignore)
  live_trader.env   the live trader's own configuration: its API key (which decides live or paper), the web
                    portal and the paper switch. The simulator never reads it. It is never put in a backup.

To use this folder with a fresh copy of the program, run:
    python live_trader.py --set-data-dir "<this folder>"

RISK WARNING: the live trader is a speculative automated trader; you can lose your entire investment.
"""

"""Live trading: many models on many currencies at once, each trading the way the simulator says it should.

RISK WARNING: this is a speculative automated trader. It buys and sells cryptocurrency on its own, and you can
lose your entire investment. Simulated results do not predict future returns. This is not financial advice.

A *deployment* is one model profile given its own capital. The trader runs any number of them side by side:
several currencies at once, and several models on each currency. Each deployment keeps its own cash, coins,
trades and tax reserve in the trading ledger, so models sharing a currency never touch each other's money.

How a deployment decides (the simulator's findings, kept exact):

    The simulator's backtest (backtest.run_backtest) is the decision engine. At every candle close, each
    deployment's model is replayed over its market's history from the moment it was deployed, exactly as a
    backtest started then would run: the same indicators, rules, regimes, friction screen, drawdown limit and
    cooldown, loss halt, order cap and tax reserves. The replay's open position is the model's *target*, and
    the trader brings the real account into line with it: it buys when the replay has just entered and sells
    when the replay is out. Between candle closes, every live price check compares the price with the stops and
    profit target the backtest would apply on the next candle, and sells the moment one is reached, as the
    backtest's intrabar stops assume. A restart replays the same history, so it reaches the same decisions.

    A buy is only placed while the replay's entry is fresh (it happened on the latest candles) and the price
    has not run away from it; a late or chased entry is skipped and the model waits for its next one. Sells are
    never skipped. Flash-crash ladders (strategy.flash_crash_buy) are not traded live yet: deployments run
    without them, as synthetic and stress runs do.

Paper and live:

    The API key decides. At start-up the trader asks Coinbase what the key may do (determine_mode): a key that
    can trade runs every deployment live, with real orders; a view-only key paper-trades them (simulated fills
    at the live price, charged the profile's fees and slippage). --paper, or FORCE_PAPER_TRADING=true in .env,
    paper-trades even with a key that can trade. Without a network the key can't be checked, so the trader
    waits, and doesn't trade, until it can.

    Live and paper keep separate records: each has its own ledger, reports and backups in its own area of the
    data folder (data_home.py), and each ledger is stamped with its mode (TradingLedger.claim_mode), so a paper
    trader can never open the live records, or the other way round, and paper money never becomes real orders.

Safeguards beyond each model's own: a HALT file beside the settings file stops new entries (exits carry on);
a combined drawdown limit across all deployments of a mode; a cap on any one order's value and on orders per
day; stale or runaway prices block buys; an order whose outcome is unknown, or an exchange balance short of
what the ledger holds, stops the deployments concerned until you review them (--clear-review).

Your data (ledger, settings, backups, logs, CSV reports) lives in a data folder outside the program, so removing
the program keeps it (data_home.py). Run it with `python live_trader.py --background`, and stop it with --stop; it runs on its own on Windows or Unix
(process_control.py; see Project_Documentation/live_trading.md).
"""

import argparse
import copy
import hashlib
import json
import logging
import logging.handlers
import math
import os
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from backtest import DEFAULT_CACHE_DIR, load_candles, lookback_window, resolve_granularity, run_backtest
from friction import FrictionModel, fetch_friction_inputs
from model_config import CompiledModel, ProfileError
import data_home
import recovery
from taxes import LONG_TERM_SECONDS

ROOT = Path(__file__).parent
LEDGER_NAME = "trading_ledger.db"
HALT_FILE = "HALT"
# Fetch a candle this long after it closes, so Coinbase has published it (as semi_live.py does).
SETTLE_SECONDS = 30
# Don't ask again for a candle Coinbase hasn't published (no trades in it) more often than this.
REFETCH_SECONDS = 60
MAX_ORDER_FAILURES = 5  # in a row, before a deployment stops for review
ORDER_RETRY_SECONDS = 60
START_RETRY_SECONDS = 60  # between attempts to start when Coinbase can't be reached
HEARTBEAT_SECONDS = 60  # how often the ledger records that the trader is alive, to measure an outage by
MAX_CLOCK_SKEW = 30  # seconds this computer's clock may differ from Coinbase's before trading waits
MAX_OUTAGES_KEPT = 50
LOGGER = logging.getLogger("quant_trading_bot.live")
# Printed whenever the trader starts.
DISCLAIMER = (
    "RISK WARNING: this is a speculative automated trader. It buys and sells cryptocurrency on its own,\n"
    "without asking you first. Cryptocurrency is extremely volatile, and you can lose your ENTIRE investment.\n"
    "Backtested, simulated and paper results do not predict future returns. This is not financial advice.\n"
    "Use it at your own risk, and only with money you can afford to lose completely.")

DEFAULT_SETTINGS = {
    "poll_seconds": 15,
    "fee_refresh_hours": 24,
    "order_timeout_seconds": 30,
    "sell_fallback_to_market": True,
    "quote_currency": "USD",
    "backup_hours": 24,
    "backup_keep": 14,
}
DEFAULT_SAFEGUARDS = {
    "max_global_drawdown": 0.25,
    "liquidate_on_global_drawdown": False,
    "max_order_value": 0,
    "max_orders_per_day": 50,
    "max_price_age_seconds": 120,
    "max_entry_delay_candles": 1,
    "max_entry_price_deviation": 0.03,
    "balance_tolerance": 0.001,
}
ORDER_STYLES = ("MARKET", "LIMIT_WITH_FRICTION_MARGIN")
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "FAILED"}


class LiveConfigError(ValueError):
    pass


class LedgerModeError(LiveConfigError):
    """A ledger belongs to the other mode (live or paper)."""


class NoNetwork(RuntimeError):
    """Coinbase couldn't be reached to check the API key."""


KEY_CHECK_RETRY_SECONDS = 60


def check_key(client, force_paper=False):
    """What the API key may do, and so which mode to trade in: ("live" or "paper", details). Raises NoNetwork
    when Coinbase can't be reached, and LiveConfigError when it refuses the key."""
    try:
        from requests.exceptions import HTTPError
    except ImportError:  # pragma: no cover - requests ships with the SDK
        HTTPError = type("_Unavailable", (Exception,), {})
    try:
        response = client.get_api_key_permissions()
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            raise LiveConfigError(f"Coinbase refused the API key (HTTP {status}); check it in .env") from None
        raise NoNetwork(f"Coinbase answered HTTP {status}: {exc}") from None
    except (OSError, ConnectionError) as exc:
        raise NoNetwork(str(exc)) from None
    except Exception as exc:  # requests' ConnectionError and Timeout, and anything else the network throws
        if type(exc).__module__.startswith(("requests", "urllib3", "socket")):
            raise NoNetwork(str(exc)) from None
        raise
    key = {name: bool(_field(response, name, default=False)) for name in ("can_view", "can_trade", "can_transfer")}
    key["portfolio_type"] = _field(response, "portfolio_type")
    key["forced_paper"] = bool(force_paper and key["can_trade"])
    mode = "live" if key["can_trade"] and not force_paper else "paper"
    if mode == "live":
        key["reason"] = "the API key can trade"
    elif key["forced_paper"]:
        key["reason"] = "paper trading was asked for (--paper or FORCE_PAPER_TRADING), although the key can trade"
    else:
        key["reason"] = "the API key is view-only"
    return mode, key


def determine_mode(client, force_paper=False, stop_event=None, retry=KEY_CHECK_RETRY_SECONDS, log=None):
    """check_key, waiting while the network is down: every `retry` seconds until it works or `stop_event` is
    set (then None). With no stop_event, the first NoNetwork is raised."""
    log = log or (lambda message: LOGGER.warning("[LIVE] %s", message))
    while True:
        try:
            return check_key(client, force_paper)
        except NoNetwork as exc:
            if stop_event is None:
                raise
            log(f"can't reach Coinbase to check the API key ({exc}); not trading, retrying in {retry}s")
            if stop_event.wait(retry):
                return None


# --- settings ---------------------------------------------------------------------------------------

def _number(value, name, low=None, high=None, allow_zero=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LiveConfigError(f"{name} must be a number, not {value!r}")
    if (low is not None and value < low) or (high is not None and value > high) or (not allow_zero and value == 0):
        raise LiveConfigError(f"{name} is {value!r}, outside its allowed range")
    return float(value)


def load_live_config(path):
    """The live trading settings file, checked, with defaults filled in. Profiles are not read here."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LiveConfigError(f"No live trading settings at {path}; copy the template "
                              f"live_config/.example/live_trading.example.json there and edit it") from None
    except ValueError as exc:
        raise LiveConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise LiveConfigError(f"{path} must hold a JSON object")
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    safeguards = {**DEFAULT_SAFEGUARDS, **(raw.get("global_safeguards") or {})}
    _number(settings["poll_seconds"], "settings.poll_seconds", low=5)
    _number(settings["order_timeout_seconds"], "settings.order_timeout_seconds", low=1)
    _number(settings["fee_refresh_hours"], "settings.fee_refresh_hours", low=0)
    _number(settings["backup_hours"], "settings.backup_hours", low=0)
    _number(settings["backup_keep"], "settings.backup_keep", low=1)
    _number(safeguards["max_global_drawdown"], "global_safeguards.max_global_drawdown", 0, 1)
    _number(safeguards["max_order_value"], "global_safeguards.max_order_value", low=0)
    _number(safeguards["max_orders_per_day"], "global_safeguards.max_orders_per_day", low=0)
    _number(safeguards["max_price_age_seconds"], "global_safeguards.max_price_age_seconds", low=1)
    _number(safeguards["max_entry_delay_candles"], "global_safeguards.max_entry_delay_candles", low=0)
    _number(safeguards["max_entry_price_deviation"], "global_safeguards.max_entry_price_deviation", 0, 1)
    _number(safeguards["balance_tolerance"], "global_safeguards.balance_tolerance", 0, 1)

    deployments, seen = [], set()
    for index, item in enumerate(raw.get("deployments") or []):
        label = f"deployments[{index}]"
        if not isinstance(item, dict):
            raise LiveConfigError(f"{label} must be an object")
        deployment_id = item.get("id")
        if not isinstance(deployment_id, str) or not deployment_id.replace("-", "_").isidentifier():
            raise LiveConfigError(f"{label}.id must be a name of letters, digits, - and _, not {deployment_id!r}")
        if deployment_id in seen:
            raise LiveConfigError(f"Two deployments are named {deployment_id!r}; each needs its own id")
        seen.add(deployment_id)
        if not isinstance(item.get("profile"), str) or not item["profile"]:
            raise LiveConfigError(f"{label}.profile must name a profile file, or best:<PRODUCT> for a "
                                  f"currency's best model")
        deployments.append({
            "id": deployment_id,
            "profile": item["profile"],
            "capital": _number(item.get("capital"), f"{label}.capital", low=0, allow_zero=False),
            "enabled": bool(item.get("enabled", True)),
            "live": item.get("live"),  # no longer used: the API key decides (kept to explain it)
            "wait_for_fresh_signal": bool(item.get("wait_for_fresh_signal", True)),
            "order_style": str(item.get("order_style", "")).upper() or None,
        })
        if deployments[-1]["order_style"] and deployments[-1]["order_style"] not in ORDER_STYLES:
            raise LiveConfigError(f"{label}.order_style must be one of {', '.join(ORDER_STYLES)}")
    return {"path": path, "settings": settings, "global_safeguards": safeguards, "deployments": deployments}


def resolve_profile_path(reference, base_dir=ROOT):
    """A deployment's profile file: a path (relative ones from `base_dir`), or best:<PRODUCT> for the model
    recorded as that currency's best in config/<PRODUCT>/best_model.json."""
    if reference.lower().startswith("best:"):
        import model_library
        product = reference.split(":", 1)[1].strip().upper()
        best = model_library.read_best(Path(base_dir) / "config").get(product)
        if best is None:
            raise LiveConfigError(f"{product} has no recorded best model (config/{product}/best_model.json)")
        return Path(best["path"])
    path = Path(reference)
    return path if path.is_absolute() else Path(base_dir) / path


def trading_profile(profile, capital):
    """(the profile a deployment trades, notes on what was changed): its capital, and no flash-crash ladder."""
    profile = copy.deepcopy(profile)
    notes = []
    if (profile.get("strategy", {}).get("flash_crash_buy") or {}).get("enabled", False):
        notes.append("flash-crash buys are not traded live yet; the model runs without its ladder")
    profile.get("strategy", {}).pop("flash_crash_buy", None)
    profile.setdefault("capital_allocation", {})["starting_capital"] = float(capital)
    return profile, notes


def profile_hash(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --- the ledger -------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, mode TEXT NOT NULL, profile_path TEXT, profile_json TEXT NOT NULL,
    profile_hash TEXT NOT NULL, capital REAL NOT NULL, started_at INTEGER NOT NULL, wait_for_fresh_signal INTEGER
    NOT NULL DEFAULT 1, status TEXT NOT NULL,
    review_note TEXT, cash REAL NOT NULL, units REAL NOT NULL DEFAULT 0, cost_basis REAL NOT NULL DEFAULT 0,
    entry_time INTEGER, entry_price REAL, entry_reason TEXT, replay_entry INTEGER, skipped_entry INTEGER,
    fees_paid REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT UNIQUE NOT NULL, deployment_id TEXT NOT NULL,
    product_id TEXT NOT NULL, mode TEXT NOT NULL, side TEXT NOT NULL, reason TEXT, order_style TEXT,
    requested_quote REAL, requested_base REAL, limit_price REAL, exchange_order_id TEXT, status TEXT NOT NULL,
    filled_base REAL DEFAULT 0, filled_value REAL DEFAULT 0, fees REAL DEFAULT 0, average_price REAL,
    error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL, product_id TEXT NOT NULL, mode TEXT NOT NULL,
    entry_time INTEGER, exit_time INTEGER NOT NULL, entry_price REAL, exit_price REAL, units REAL, cost REAL,
    proceeds REAL, fees REAL, pnl REAL, entry_reason TEXT, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL, level TEXT NOT NULL, deployment_id TEXT,
    message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
    time INTEGER NOT NULL, deployment_id TEXT NOT NULL, price REAL, equity REAL, replay_equity REAL,
    PRIMARY KEY (time, deployment_id));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
POSITION_FIELDS = ("status", "review_note", "cash", "units", "cost_basis", "entry_time", "entry_price",
                   "entry_reason", "replay_entry", "skipped_entry", "fees_paid")


class TradingLedger:
    """The trading record: deployments and their money, every order, closed trade and event (SQLite).

    Orders are written before they are sent and again when they finish, with full sync, so a crash at any
    point leaves a record to recover from: real money is worth the disk writes.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.execute("PRAGMA journal_mode=WAL;")
            self.connection.execute("PRAGMA synchronous=FULL;")
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def close(self):
        with self._lock:
            self.connection.close()

    def _write(self, sql, args=()):
        with self._lock:
            cursor = self.connection.execute(sql, args)
            self.connection.commit()
            return cursor.lastrowid

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    # deployments
    def deployment(self, deployment_id):
        rows = self._rows("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        return rows[0] if rows else None

    def deployments(self):
        return self._rows("SELECT * FROM deployments ORDER BY id")

    def create_deployment(self, deployment_id, product_id, mode, profile_path, profile, capital, started_at,
                          wait_for_fresh_signal=True):
        self._write(
            "INSERT INTO deployments (id, product_id, mode, profile_path, profile_json, profile_hash, capital, "
            "started_at, wait_for_fresh_signal, status, cash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
            (deployment_id, product_id, mode, str(profile_path), json.dumps(profile), profile_hash(profile),
             float(capital), int(started_at), int(bool(wait_for_fresh_signal)), float(capital), int(started_at)))
        return self.deployment(deployment_id)

    def save_position(self, deployment_id, now, **fields):
        unknown = set(fields) - set(POSITION_FIELDS)
        if unknown:
            raise ValueError(f"Unknown deployment fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._write(f"UPDATE deployments SET {assignments}, updated_at = ? WHERE id = ?",
                    (*fields.values(), int(now), deployment_id))

    # orders
    def record_order(self, now, **fields):
        fields = {**fields, "created_at": int(now), "updated_at": int(now)}
        names = ", ".join(fields)
        self._write(f"INSERT INTO orders ({names}) VALUES ({', '.join('?' for _ in fields)})", tuple(fields.values()))

    def update_order(self, client_order_id, now, **fields):
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._write(f"UPDATE orders SET {assignments}, updated_at = ? WHERE client_order_id = ?",
                    (*fields.values(), int(now), client_order_id))

    def settle_order(self, now, deployment_id, position, client_order_id=None, order=None, trade=None):
        """In one transaction: an order's outcome, the deployment's new position and the trade it closed. A
        crash can't leave an order recorded as filled without its position, or the other way round."""
        unknown = set(position) - set(POSITION_FIELDS)
        if unknown:
            raise ValueError(f"Unknown deployment fields: {sorted(unknown)}")
        with self._lock, self.connection:
            if client_order_id is not None:
                self.connection.execute(
                    f"UPDATE orders SET {', '.join(f'{name} = ?' for name in order)}, updated_at = ? "
                    f"WHERE client_order_id = ?", (*order.values(), int(now), client_order_id))
            self.connection.execute(
                f"UPDATE deployments SET {', '.join(f'{name} = ?' for name in position)}, updated_at = ? "
                f"WHERE id = ?", (*position.values(), int(now), deployment_id))
            if trade is not None:
                self.connection.execute(f"INSERT INTO trades ({', '.join(trade)}) VALUES "
                                        f"({', '.join('?' for _ in trade)})", tuple(trade.values()))

    def order(self, client_order_id):
        rows = self._rows("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return rows[0] if rows else None

    def unfinished_orders(self):
        return self._rows("SELECT * FROM orders WHERE status IN ('SUBMITTING', 'SUBMITTED', 'UNKNOWN') "
                          "ORDER BY id")

    def orders_since(self, since, mode=None):
        if mode is None:
            return self._rows("SELECT * FROM orders WHERE created_at >= ? ORDER BY id", (int(since),))
        return self._rows("SELECT * FROM orders WHERE created_at >= ? AND mode = ? ORDER BY id", (int(since), mode))

    def recent_orders(self, limit=50):
        return self._rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),))

    def recent_trades(self, limit=50):
        return self._rows("SELECT * FROM trades ORDER BY exit_time DESC, id DESC LIMIT ?", (int(limit),))

    def equity_history(self, deployment_id, limit=500):
        """The deployment's real value and its replay's at its latest `limit` candle closes, newest first."""
        return self._rows("SELECT time, price, equity, replay_equity FROM equity WHERE deployment_id = ? "
                          "ORDER BY time DESC LIMIT ?", (deployment_id, int(limit)))

    # trades, events, equity, meta
    def record_trade(self, **fields):
        names = ", ".join(fields)
        self._write(f"INSERT INTO trades ({names}) VALUES ({', '.join('?' for _ in fields)})", tuple(fields.values()))

    def trades(self, deployment_id=None):
        if deployment_id is None:
            return self._rows("SELECT * FROM trades ORDER BY exit_time, id")
        return self._rows("SELECT * FROM trades WHERE deployment_id = ? ORDER BY exit_time, id", (deployment_id,))

    def log_event(self, now, level, message, deployment_id=None):
        self._write("INSERT INTO events (time, level, deployment_id, message) VALUES (?, ?, ?, ?)",
                    (int(now), level, deployment_id, message))

    def events(self, limit=50):
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))

    def record_equity(self, now, deployment_id, price, equity, replay_equity):
        self._write("INSERT OR REPLACE INTO equity (time, deployment_id, price, equity, replay_equity) "
                    "VALUES (?, ?, ?, ?, ?)", (int(now), deployment_id, price, equity, replay_equity))

    def claim_mode(self, mode):
        """Stamp the ledger "live" or "paper" on first use; refuse it to a trader of the other mode, so paper
        records can never drive real orders (or real records be paper-traded)."""
        stamped = self.get_meta("ledger_mode")
        if stamped is None:
            other = {row["mode"] for row in self.deployments()} - {mode}
            if other:
                raise LedgerModeError(f"{self.path} holds {', '.join(sorted(other))} deployments; a {mode} trader "
                                      f"won't use it")
            self.set_meta("ledger_mode", mode)
        elif stamped != mode:
            raise LedgerModeError(f"{self.path} is the {stamped} trading ledger; a {mode} trader won't use it")
        return mode

    def get_meta(self, key, default=None):
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_meta(self, key, value):
        if value is None:
            self._write("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._write("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))


# --- the exchange -----------------------------------------------------------------------------------

def _field(obj, *path, default=None):
    """A field of an SDK response or a plain dict, following `path`."""
    for key in path:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key)
            continue
        value = getattr(obj, key, None)
        if value is None and hasattr(obj, "to_dict"):
            try:
                value = obj.to_dict().get(key)
            except Exception:  # pragma: no cover - SDK object without a usable to_dict
                value = None
        obj = value
    return default if obj is None else obj


def _float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def floor_to(value, increment):
    """`value` rounded down to a multiple of `increment` (a string or number; 0 or None leaves it)."""
    step = Decimal(str(increment or 0))
    if step <= 0:
        return float(value)
    return float((Decimal(str(value)) / step).to_integral_value(ROUND_DOWN) * step)


def _decimal_text(value):
    return format(Decimal(str(value)).normalize(), "f")


class MarketData:
    """Live prices and product rules from Coinbase. Both work with a view-only key."""

    def __init__(self, client, product_ttl=3600):
        self.client, self.product_ttl = client, product_ttl
        self._products = {}

    def quotes(self, product_ids, now):
        """{product: {"bid", "ask", "mid", "time"}} for every product with a two-sided book."""
        quotes = {}
        try:
            response = self.client.get_best_bid_ask(product_ids=list(product_ids))
            for book in _field(response, "pricebooks", default=[]) or []:
                bids, asks = _field(book, "bids", default=[]), _field(book, "asks", default=[])
                if not bids or not asks:
                    continue
                bid, ask = _float(_field(bids[0], "price")), _float(_field(asks[0], "price"))
                if bid > 0 and ask > 0:
                    quotes[_field(book, "product_id")] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "time": now}
        except Exception as exc:
            LOGGER.warning("[LIVE] Best bid/ask request failed: %s", exc)
        for product_id in product_ids:
            if product_id in quotes:
                continue
            info = self.product(product_id, now, refresh=True)
            price = _float(info.get("price"))
            if price > 0:
                quotes[product_id] = {"bid": price, "ask": price, "mid": price, "time": now}
        return quotes

    def product(self, product_id, now, refresh=False):
        """The product's trading rules: increments, minimum sizes, status and last price ({} if unreadable)."""
        cached = self._products.get(product_id)
        if cached and not refresh and now - cached["read_at"] < self.product_ttl:
            return cached
        try:
            product = self.client.get_product(product_id=product_id)
        except Exception as exc:
            LOGGER.warning("[LIVE] Could not read %s's trading rules: %s", product_id, exc)
            return cached or {}
        info = {"read_at": now, "price": _field(product, "price"),
                "base_increment": _field(product, "base_increment"),
                "quote_increment": _field(product, "quote_increment"),
                "base_min_size": _float(_field(product, "base_min_size")),
                "quote_min_size": _float(_field(product, "quote_min_size")),
                "trading_disabled": bool(_field(product, "trading_disabled", default=False)),
                "cancel_only": bool(_field(product, "cancel_only", default=False)),
                "limit_only": bool(_field(product, "limit_only", default=False)),
                "status": str(_field(product, "status", default="online")).lower()}
        self._products[product_id] = info
        return info


class PaperBroker:
    """Simulated fills at the live price, charged as the simulator charges them: the profile's slippage on
    the price and its taker fee on the value."""

    mode = "paper"

    def buy(self, product_id, quote_amount, quote, client_order_id, order_style, slippage, fee_rate, info):
        price = quote["mid"] * (1 + slippage)
        fee = quote_amount * fee_rate
        return {"status": "FILLED", "base": (quote_amount - fee) / price, "value": quote_amount - fee, "fees": fee,
                "price": price, "order_id": f"paper-{client_order_id}"}

    def sell(self, product_id, base_amount, quote, client_order_id, order_style, slippage, fee_rate, info):
        price = quote["mid"] * (1 - slippage)
        value = base_amount * price
        return {"status": "FILLED", "base": base_amount, "value": value, "fees": value * fee_rate, "price": price,
                "order_id": f"paper-{client_order_id}"}


class OrderUnknown(Exception):
    """An order may or may not have reached the exchange; only a person should decide what happened."""

    def __init__(self, message, order_id=None):
        super().__init__(message)
        self.order_id = order_id


class CoinbaseBroker:
    """Real orders on Coinbase Advanced Trade.

    MARKET orders fill at once at the book (the simulator's taker fill). LIMIT_WITH_FRICTION_MARGIN sends an
    immediate-or-cancel limit order at the best price plus the profile's slippage_buffer, so a fill never costs
    more slippage than the backtest charged; a buy that can't fill within it is left unfilled, and a sell's
    unfilled rest goes to the market (sell_fallback_to_market), since an exit must happen.
    """

    mode = "live"

    def __init__(self, client, order_timeout=30, sell_fallback_to_market=True, sleep=time.sleep, clock=time.time,
                 on_submitted=None):
        self.client, self.order_timeout = client, order_timeout
        self.sell_fallback_to_market, self.sleep, self.clock = sell_fallback_to_market, sleep, clock
        # Called with (client_order_id, exchange_order_id) as soon as the exchange accepts an order.
        self.on_submitted = on_submitted or (lambda client_order_id, order_id: None)

    def balances(self):
        """{currency: (available, total)} for every account."""
        balances, cursor = {}, None
        while True:
            response = self.client.get_accounts(limit=250, cursor=cursor) if cursor else self.client.get_accounts(
                limit=250)
            for account in _field(response, "accounts", default=[]) or []:
                currency = _field(account, "currency")
                available = _float(_field(account, "available_balance", "value"))
                hold = _float(_field(account, "hold", "value"))
                before = balances.get(currency, (0.0, 0.0))
                balances[currency] = (before[0] + available, before[1] + available + hold)
            cursor = _field(response, "cursor")
            if not _field(response, "has_next", default=False) or not cursor:
                return balances

    def _submit(self, client_order_id, create):
        """Send an order; its exchange id. Raises OrderUnknown if it may have been placed, ValueError if not."""
        try:
            from requests.exceptions import HTTPError
        except ImportError:  # pragma: no cover - requests ships with the SDK
            HTTPError = type("_Unavailable", (Exception,), {})
        try:
            response = create()
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status < 500:
                raise ValueError(f"Coinbase rejected the order (HTTP {status}): {exc}") from None
            raise OrderUnknown(f"Coinbase did not answer clearly (HTTP {status}): {exc}") from None
        except Exception as exc:
            raise OrderUnknown(f"The order request failed part-way: {exc}") from None
        if not _field(response, "success", default=False):
            error = _field(response, "error_response", default={})
            raise ValueError(f"Coinbase rejected the order: {_field(error, 'message', default='') or error}")
        order_id = _field(response, "success_response", "order_id") or _field(response, "order_id")
        if not order_id:
            raise OrderUnknown("Coinbase accepted the order without an order id")
        self.on_submitted(client_order_id, order_id)
        return order_id

    def await_order(self, order_id):
        """Poll the order until it finishes; its fill. Cancels one still open at the timeout."""
        deadline = self.clock() + self.order_timeout
        cancelled = False
        while True:
            try:
                order = _field(self.client.get_order(order_id=order_id), "order")
            except Exception as exc:
                order = None
                LOGGER.warning("[LIVE] Could not read order %s: %s", order_id, exc)
            status = str(_field(order, "status", default="")).upper()
            if status in TERMINAL_STATUSES:
                return self._fill(order, order_id)
            if self.clock() >= deadline:
                if cancelled:
                    raise OrderUnknown(f"Order {order_id} is still {status or 'unreadable'} after cancelling it",
                                       order_id)
                try:
                    self.client.cancel_orders(order_ids=[order_id])
                except Exception as exc:
                    LOGGER.warning("[LIVE] Could not cancel order %s: %s", order_id, exc)
                cancelled, deadline = True, self.clock() + self.order_timeout
            self.sleep(1.0)

    @staticmethod
    def _fill(order, order_id):
        base = _float(_field(order, "filled_size"))
        value = _float(_field(order, "filled_value"))
        price = _float(_field(order, "average_filled_price"))
        if not value and base and price:
            value = base * price
        status = str(_field(order, "status", default="")).upper()
        return {"status": "FILLED" if status == "FILLED" else ("PARTIAL" if base > 0 else "UNFILLED"),
                "base": base, "value": value, "fees": _float(_field(order, "total_fees")),
                "price": price or (value / base if base else 0.0), "order_id": order_id, "exchange_status": status}

    def buy(self, product_id, quote_amount, quote, client_order_id, order_style, slippage, fee_rate, info):
        if order_style == "LIMIT_WITH_FRICTION_MARGIN" and not info.get("cancel_only"):
            limit = floor_to(quote["ask"] * (1 + slippage), info.get("quote_increment"))
            base = floor_to(quote_amount / (limit * (1 + fee_rate)), info.get("base_increment"))
            if base <= 0:
                return {"status": "UNFILLED", "base": 0.0, "value": 0.0, "fees": 0.0, "price": 0.0, "order_id": None}
            order_id = self._submit(client_order_id, lambda: self.client.limit_order_ioc_buy(
                client_order_id=client_order_id, product_id=product_id, base_size=_decimal_text(base),
                limit_price=_decimal_text(limit)))
        else:
            size = floor_to(quote_amount, info.get("quote_increment"))
            order_id = self._submit(client_order_id, lambda: self.client.market_order_buy(
                client_order_id=client_order_id, product_id=product_id, quote_size=_decimal_text(size)))
        return self.await_order(order_id)

    def sell(self, product_id, base_amount, quote, client_order_id, order_style, slippage, fee_rate, info):
        base_amount = floor_to(base_amount, info.get("base_increment"))
        fills = []
        if order_style == "LIMIT_WITH_FRICTION_MARGIN":
            limit = floor_to(quote["bid"] * (1 - slippage), info.get("quote_increment"))
            order_id = self._submit(client_order_id, lambda: self.client.limit_order_ioc_sell(
                client_order_id=client_order_id, product_id=product_id, base_size=_decimal_text(base_amount),
                limit_price=_decimal_text(limit)))
            fills.append(self.await_order(order_id))
            rest = floor_to(base_amount - fills[-1]["base"], info.get("base_increment"))
            if not self.sell_fallback_to_market or rest <= 0 or rest < info.get("base_min_size", 0):
                return fills[0] if len(fills) == 1 else combine_fills(fills)
            base_amount, client_order_id = rest, f"{client_order_id}-m"
        order_id = self._submit(client_order_id, lambda: self.client.market_order_sell(
            client_order_id=client_order_id, product_id=product_id, base_size=_decimal_text(base_amount)))
        fills.append(self.await_order(order_id))
        return combine_fills(fills)


def combine_fills(fills):
    base = sum(fill["base"] for fill in fills)
    value = sum(fill["value"] for fill in fills)
    statuses = [fill["status"] for fill in fills]
    status = "FILLED" if statuses[-1] == "FILLED" else ("PARTIAL" if base > 0 else "UNFILLED")
    return {"status": status, "base": base, "value": value, "fees": sum(fill["fees"] for fill in fills),
            "price": value / base if base else 0.0,
            "order_id": ",".join(str(fill.get("order_id")) for fill in fills if fill.get("order_id"))}


def order_outcome(fill):
    """An order record's fields for a fill."""
    return {"status": fill["status"], "filled_base": fill["base"], "filled_value": fill["value"],
            "fees": fill["fees"], "average_price": fill["price"],
            **({"exchange_order_id": fill["order_id"]} if fill.get("order_id") else {})}


# --- one deployment ---------------------------------------------------------------------------------

class Deployment:
    """One model with its own capital: its settings, its money (from the ledger) and its latest replay."""

    def __init__(self, record, requested=None, notes=(), source_hash=None):
        self.id, self.product_id, self.mode = record["id"], record["product_id"], record["mode"]
        self.profile = json.loads(record["profile_json"])
        self.capital, self.started_at = record["capital"], record["started_at"]
        self.requested = requested  # this run's settings entry, or None when it is no longer in the settings
        self.wait = bool(record["wait_for_fresh_signal"])  # fixed at deployment: it changes the replay's decisions
        self.order_style = ((requested or {}).get("order_style") or
                            str(self.profile.get("execution", {}).get("order_style", "MARKET")).upper())
        if self.order_style not in ORDER_STYLES:
            self.order_style = "MARKET"
        self.granularity, self.seconds = resolve_granularity(self.profile.get("period_granularity", "FIFTEEN_MINUTE"))
        self.market = (self.product_id, self.granularity)
        self.base_currency = self.product_id.partition("-")[0]
        self.notes = list(notes)
        if source_hash and source_hash != record["profile_hash"]:
            self.notes.append("its profile file has changed since it was deployed; it still trades the deployed "
                              "version (deploy under a new id to trade the change)")
        if requested and bool(requested["wait_for_fresh_signal"]) != self.wait:
            self.notes.append(f"wait_for_fresh_signal stays {str(self.wait).lower()}, as it was deployed")
        self.friction = FrictionModel(self.profile)
        self.replay = None  # the last replay's summary
        self.replay_error = None
        self.failures = 0
        self.retry_after = 0.0
        self.load(record)

    def load(self, record):
        for name in POSITION_FIELDS:
            setattr(self, name, record[name])

    @property
    def enabled(self):
        return self.requested is not None and self.requested["enabled"]

    @property
    def in_position(self):
        return self.entry_time is not None and self.units > 0

    def equity(self, price):
        return self.cash + self.units * (price or 0.0)

    def position_fields(self):
        return {"cash": self.cash, "units": self.units, "cost_basis": self.cost_basis, "fees_paid": self.fees_paid,
                "entry_time": self.entry_time, "entry_price": self.entry_price, "entry_reason": self.entry_reason,
                "replay_entry": self.replay_entry}


def tax_state(deployment, trades, now):
    """(tax set aside, tax paid) on a deployment's closed trades, under its profile's tax rule (taxes.py)."""
    ledger = deployment.friction.tax_ledger()
    ledger.start(deployment.started_at)
    for trade in trades:
        ledger.settle(trade["exit_time"])
        held = trade["exit_time"] - (trade["entry_time"] or trade["exit_time"])
        ledger.realize(trade["pnl"], trade["exit_time"], long_term=held > LONG_TERM_SECONDS)
    ledger.settle(now)
    return ledger.reserved, ledger.paid


# --- the trader -------------------------------------------------------------------------------------

class LiveTrader:
    """Runs every deployment in the settings, on every currency they trade, in one loop.

    `client` is a Coinbase REST client (a view-only key is enough for paper trading); `env` the .env settings
    (engine.load_environment_config). `live_broker` places real orders (a CoinbaseBroker by default).
    """

    def __init__(self, config, client, ledger, env, base_dir=ROOT, cache_dir=DEFAULT_CACHE_DIR, now=time.time,
                 live_broker=None, paper_broker=None, market_data=None, data_dir=None, mode="paper", key=None):
        self.config, self.client, self.ledger, self.env = config, client, ledger, env
        self.settings, self.safeguards = config["settings"], config["global_safeguards"]
        self.base_dir, self.cache_dir, self.now = Path(base_dir), cache_dir, now
        # The data folder (data_home.py): backups and readable reports go here, outside the program.
        if mode not in data_home.MODES:
            raise ValueError(f"mode must be one of {data_home.MODES}, not {mode!r}")
        # "live" (real orders) or "paper", chosen from what the API key may do (check_key); every deployment this
        # trader runs is in this mode, and its records are this mode's own.
        self.mode, self.key = mode, key
        self.paths = data_home.DataPaths(data_dir or base_dir, mode)
        self.market_data = market_data or MarketData(client)
        self.paper_broker = paper_broker or PaperBroker()
        self.live_broker = live_broker or CoinbaseBroker(
            client, order_timeout=self.settings["order_timeout_seconds"],
            sell_fallback_to_market=self.settings["sell_fallback_to_market"],
            on_submitted=lambda client_order_id, order_id: self.ledger.update_order(
                client_order_id, self.now(), exchange_order_id=order_id, status="SUBMITTED"))
        self.deployments = {}
        self.blocked = {}  # deployment id -> why it can't run
        self.markets = {}  # (product, granularity) -> {"seconds", "last", "tried"}
        self.quotes = {}
        self.fees_read_at = None
        self.fee_inputs = None
        self.last_poll = 0.0
        self._snapshot = {}
        self.state = "starting"  # until the first decisions are made; then "running"
        self._snapshot_lock = threading.Lock()
        self.halt_path = Path(config["path"]).parent / HALT_FILE

    # logging
    def event(self, level, message, deployment_id=None):
        getattr(LOGGER, level.lower(), LOGGER.info)("[LIVE] %s%s", f"{deployment_id}: " if deployment_id else "",
                                                     message)
        self.ledger.log_event(self.now(), level.upper(), message, deployment_id)

    # set-up
    def system_live(self):
        """Whether this trader places real orders: its API key can trade (see check_key)."""
        return self.mode == "live"

    def load_deployments(self):
        """Create or reopen every deployment in the settings, and keep any no longer listed that still hold a
        position, so they can wind down."""
        now = self.now()
        listed = {item["id"]: item for item in self.config["deployments"]}
        for deployment_id, item in listed.items():
            try:
                self._open_deployment(item, now)
            except (LiveConfigError, ProfileError, ValueError, OSError) as exc:
                self.blocked[deployment_id] = str(exc)
                self.event("ERROR", f"cannot run: {exc}", deployment_id)
        for record in self.ledger.deployments():
            if record["id"] in listed or record["id"] in self.deployments:
                continue
            if record["units"] > 0 and record["entry_time"] is not None:
                self.deployments[record["id"]] = Deployment(record)
                self.event("WARNING", "no longer in the settings but holds a position; winding it down", record["id"])
        for deployment in self.deployments.values():
            self.markets.setdefault(deployment.market, {"seconds": deployment.seconds, "last": None, "tried": 0.0})

    def _open_deployment(self, item, now):
        record = self.ledger.deployment(item["id"])
        try:
            path = resolve_profile_path(item["profile"], self.base_dir)
            source = json.loads(path.read_text(encoding="utf-8"))
        except (LiveConfigError, OSError, ValueError) as exc:
            if record is None:
                raise
            # Already deployed: the ledger holds the exact profile it trades, so a machine it was moved to (or a
            # profile file deleted since) doesn't stop it.
            if record["mode"] != self.mode:
                raise LedgerModeError(f"is a {record['mode']} deployment in a {self.mode} trader's records")
            deployment = Deployment(record, item, [f"its profile file can't be read ({exc}); it trades the copy "
                                                   f"stored in the ledger when it was deployed"])
            self.deployments[deployment.id] = deployment
            return
        if not isinstance(source, dict) or "product_id" not in source:
            raise LiveConfigError(f"{path} is not a model profile with a product_id")
        profile, notes = trading_profile(source, item["capital"])
        if profile.get("strategy", {}).get("direction", "LONG_ONLY") != "LONG_ONLY":
            raise LiveConfigError("only LONG_ONLY strategies can trade")
        _, seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        CompiledModel(profile, seconds)  # every problem with its indicators and rules, before any money moves
        if item.get("live") is not None:
            notes.append('its "live" setting is no longer used: the API key decides (live with a key that can '
                         'trade, paper with a view-only key)')
        if record is None and not item["enabled"]:
            return  # never deployed, and not wanted yet
        if record is None:
            mode = self.mode
            record = self.ledger.create_deployment(item["id"], profile["product_id"], mode, path, profile,
                                                   item["capital"], now, item["wait_for_fresh_signal"])
            self.event("INFO", f"deployed {path.name} on {profile['product_id']} with {item['capital']:,.2f} "
                               f"({mode})", item["id"])
        elif record["mode"] != self.mode:
            raise LedgerModeError(f"is a {record['mode']} deployment in a {self.mode} trader's records")
        deployment = Deployment(record, item, notes,
                                profile_hash(trading_profile(source, record["capital"])[0]))
        self.deployments[deployment.id] = deployment

    def refresh_fees(self, force=False):
        """The live fee tier and network fee, read once at start-up and every fee_refresh_hours."""
        now = self.now()
        hours = self.settings["fee_refresh_hours"]
        if not force and self.fees_read_at is not None and (not hours or now - self.fees_read_at < hours * 3600):
            return
        profiles = [deployment.profile for deployment in self.deployments.values()]
        if not profiles:
            return
        self.fee_inputs = fetch_friction_inputs(self.client, profiles)
        self.fees_read_at = now
        for deployment in self.deployments.values():
            deployment.friction = FrictionModel(deployment.profile, self.fee_inputs)
        if self.fee_inputs.get("exchange_fee_error"):
            self.event("WARNING", f"live fee tier unavailable, using each profile's fallback rate: "
                                  f"{self.fee_inputs['exchange_fee_error']}")

    def start(self):
        """Check the clock, note any outage, load the deployments, settle unfinished orders, check balances and
        make the first decisions. After an outage this is all it takes to resume: every model is replayed from
        when it was deployed, so an exit missed while offline is sold at once and an entry missed is skipped."""
        self.ledger.claim_mode(self.mode)
        self.check_clock()
        self.begin_session()
        self.load_deployments()
        self.check_tax_profile()
        settled = self.recover_orders()
        self.refresh_fees(force=True)
        self.check_balances()
        self.state = "running"
        self.step(first=True)
        holding = sum(1 for d in self.deployments.values() if d.in_position)
        self.event("INFO", f"trading: {len(self.deployments)} deployments on {len(self.markets)} markets, "
                           f"{holding} holding a position" + (f", {settled} unfinished orders settled from the "
                                                               f"exchange" if settled else ""))

    # sessions, outages and backups
    def check_clock(self):
        """Refuse to trade on a wrong clock (common after a power cut): candles and orders are timed by it."""
        getter = getattr(self.client, "get_unix_time", None)
        if getter is None:
            return
        try:
            server = _float(_field(getter(), "epoch_seconds"), None)
        except Exception as exc:  # can't tell: Coinbase's own requests will fail too if it is far off
            LOGGER.warning("[LIVE] Could not read Coinbase's clock: %s", exc)
            return
        if server is None:
            return
        skew = self.now() - server
        if abs(skew) > MAX_CLOCK_SKEW:
            raise RuntimeError(f"this computer's clock is {skew:+.0f}s off Coinbase's; set the clock right (turn on "
                               f"automatic time) and the trader will start")

    def begin_session(self):
        """Record this run in the ledger, and if the last one never stopped cleanly (a power cut, a crash, a
        killed process), record the outage: when it was last seen, and how long it was offline."""
        if getattr(self, "_session", None):
            return  # a retried start: still the same session
        now = self.now()
        previous = self.ledger.get_meta("session")
        host = socket.gethostname()
        self.recovery = {"resumed_at": int(now), "outage": None, "moved_from": None, "previous": previous}
        if previous and not previous.get("clean"):
            outage = {"last_seen": previous.get("heartbeat"), "resumed": int(now),
                      "seconds": int(now - (previous.get("heartbeat") or now)), "host": previous.get("host")}
            outages = (self.ledger.get_meta("outages") or [])[-(MAX_OUTAGES_KEPT - 1):] + [outage]
            self.ledger.set_meta("outages", outages)
            self.recovery["outage"] = outage
            self.event("WARNING", f"resuming after an unexpected stop: last seen "
                                  f"{recovery._when(outage['last_seen'] or now)}, offline "
                                  f"{recovery.describe_seconds(outage['seconds'])}. Replaying every model to catch up")
        if previous and previous.get("host") and previous["host"] != host:
            self.recovery["moved_from"] = previous["host"]
            self.event("INFO", f"this ledger last ran on {previous['host']}; now running on {host}")
        self._session = {"started": int(now), "heartbeat": int(now), "clean": False, "host": host,
                         "pid": os.getpid()}
        self.ledger.set_meta("session", self._session)
        if self.ledger.deployments():
            self.backup(now, "before resuming")

    def heartbeat(self, now):
        session = getattr(self, "_session", None)
        if session and now - session["heartbeat"] >= HEARTBEAT_SECONDS:
            session["heartbeat"] = int(now)
            self.ledger.set_meta("session", session)

    def end_session(self):
        """A clean stop: back up, and record that the next start isn't recovering from an outage."""
        session = getattr(self, "_session", None)
        if not session:
            return
        now = self.now()
        self.backup(now, "at a clean stop")
        session.update(heartbeat=int(now), clean=True, ended=int(now))
        self.ledger.set_meta("session", session)

    def backup(self, now, why="scheduled"):
        """Write a recovery bundle to the data folder's backups/ (see recovery.py), and refresh the readable
        reports; a failure is logged, never fatal."""
        import taxes
        try:
            path = recovery.automatic_backup(self.paths.backups, self.base_dir, self.ledger.path,
                                             self.config["path"], now, keep=int(self.settings["backup_keep"]),
                                             tax_profile=taxes.PROFILE_FILE if taxes.PROFILE_FILE.exists() else None,
                                             mode=self.mode)
        except Exception as exc:
            self.event("WARNING", f"backup failed ({why}): {exc}")
            return None
        self.ledger.set_meta("last_backup", {"time": int(now), "path": str(path)})
        LOGGER.info("[LIVE] Backup written %s: %s", why, path)
        self.export_reports()
        return path

    def export_reports(self):
        """Rewrite the CSV copies of the ledger in the data folder's reports/, readable without the program."""
        try:
            skipped = recovery.export_reports(self.ledger.path, self.paths.reports)
        except Exception as exc:
            LOGGER.warning("[LIVE] Could not write the reports: %s", exc)
            return
        if skipped:
            LOGGER.info("[LIVE] %s open elsewhere (a spreadsheet?); rewritten next time", ", ".join(skipped))

    def maybe_backup(self, now):
        hours = self.settings["backup_hours"]
        last = (self.ledger.get_meta("last_backup") or {}).get("time")
        if hours and (last is None or now - last >= hours * 3600):
            self.backup(now)

    def check_tax_profile(self):
        """Warn when deployments are taxed progressively but the private tax profile is missing (often after a
        move): reserves would then follow the example profile's salary."""
        import taxes
        progressive = [d.id for d in self.deployments.values()
                       if d.friction.tax_model == "progressive" and d.friction.tax_rate]
        if progressive and not taxes.PROFILE_FILE.exists():
            self.event("WARNING", f"{taxes.PROFILE_FILE} is missing, so tax reserves for {', '.join(progressive)} "
                                  f"use the example profile's salary; restore it from your backup")

    # the loop
    def run(self, stop_event):
        """Trade until `stop_event` (a threading.Event) is set. A start that fails (Coinbase unreachable, say) is
        retried every START_RETRY_SECONDS, so a node that boots before its network keeps trying."""
        self.publish()  # "starting": the portal shows the trader is up before the first decisions
        while not stop_event.is_set():
            try:
                self.start()
                break
            except Exception as exc:
                LOGGER.exception("[LIVE] Start failed")
                self.event("ERROR", f"could not start, retrying in {START_RETRY_SECONDS}s: {exc}")
                stop_event.wait(START_RETRY_SECONDS)
        try:
            while not stop_event.wait(self.settings["poll_seconds"]):
                try:
                    self.step()
                except Exception as exc:  # one bad cycle mustn't end the trader
                    LOGGER.exception("[LIVE] Cycle failed")
                    self.event("ERROR", f"cycle failed: {exc}")
        finally:
            self.end_session()

    def step(self, first=False):
        """One cycle: read prices, act on stops, and at each candle close replay and line up every model."""
        now = self.now()
        self.check_reviews()
        self.refresh_fees()
        self.poll(now)
        for market, info in self.markets.items():
            if first or self._candle_due(info, now):
                self.update_market(market, now)
        self.check_global_drawdown(self.now())
        self.heartbeat(self.now())
        self.maybe_backup(self.now())
        self.publish()

    def poll(self, now):
        """Read every product's price and sell any position whose stop or profit target it has reached."""
        products = sorted({deployment.product_id for deployment in self.deployments.values()})
        if products:
            self.quotes.update(self.market_data.quotes(products, now))
        self.last_poll = now
        for deployment in self.deployments.values():
            self._guarded(deployment, self.check_exits, deployment, now)

    def _guarded(self, deployment, action, *args):
        """Run one deployment's step so that its failure is logged and the other deployments carry on."""
        try:
            action(*args)
        except Exception as exc:
            LOGGER.exception("[LIVE] %s failed", deployment.id)
            deployment.retry_after = self.now() + ORDER_RETRY_SECONDS
            self.event("ERROR", f"{action.__name__} failed: {exc}", deployment.id)

    def _maybe_poll(self):
        """Check prices between replays when a poll is due, so a slow replay can't delay a stop."""
        now = self.now()
        if now - self.last_poll >= self.settings["poll_seconds"]:
            self.poll(now)

    def _candle_due(self, info, now):
        open_candle = int(now - SETTLE_SECONDS) // info["seconds"] * info["seconds"]
        due = info["last"] is None or open_candle - info["seconds"] > info["last"]
        return due and now - info["tried"] >= REFETCH_SECONDS

    def update_market(self, market, now):
        """Fetch the market's newly closed candles, then replay and line up each of its deployments."""
        info = self.markets[market]
        info["tried"] = now
        try:
            start, end = lookback_window(info["seconds"],
                                         now=datetime.fromtimestamp(now - SETTLE_SECONDS, timezone.utc))
            candles = load_candles(self.client, *market, start, end, cache_dir=self.cache_dir)
        except Exception as exc:
            self.event("WARNING", f"could not fetch {market[0]} {market[1]} candles: {exc}")
            return
        if not candles:
            return
        info["last"] = candles[-1]["start"]
        for deployment in [d for d in self.deployments.values() if d.market == market]:
            self.replay(deployment, candles)
            self._guarded(deployment, self.reconcile, deployment, self.now())
            self._maybe_poll()

    def replay(self, deployment, candles):
        """Run the deployment's model through the backtest from when it was deployed; keep where it stands."""
        if candles[-1]["start"] < deployment.started_at:
            deployment.replay = None
            deployment.replay_error = None
            return
        try:
            result = run_backtest(candles, deployment.profile, starting_capital=deployment.capital,
                                  friction=deployment.friction, start_time=deployment.started_at,
                                  wait_for_fresh_signal=deployment.wait)
        except Exception as exc:
            deployment.replay, deployment.replay_error = None, str(exc)
            self.event("ERROR", f"replay failed: {exc}", deployment.id)
            return
        last_exit = result["trades"][-1] if result["trades"] else None
        deployment.replay_error = None
        deployment.replay = {
            "state": result["live_state"], "equity": result["ending_equity"], "return": result["total_return"],
            "trade_count": result["trade_count"], "last_close": candles[-1]["close"],
            "last_candle": candles[-1]["start"], "screened_signals": result["screened_signals"],
            "last_exit": None if last_exit is None else {"time": last_exit["exit_time"],
                                                         "reason": last_exit["exit_reason"]},
        }
        self.ledger.record_equity(candles[-1]["start"] + deployment.seconds, deployment.id, candles[-1]["close"],
                                  deployment.equity(candles[-1]["close"]), result["ending_equity"])

    # deciding
    def status_of(self, deployment):
        """What the deployment may do now: ACTIVE, WINDING_DOWN, INACTIVE, NEEDS_REVIEW or HALTED."""
        if deployment.status == "NEEDS_REVIEW":
            return "NEEDS_REVIEW"
        if deployment.replay and deployment.replay["state"]["halted"] and not deployment.in_position:
            return "HALTED"
        if not deployment.enabled:
            return "WINDING_DOWN" if deployment.in_position else "INACTIVE"
        return "ACTIVE"

    def entries_blocked(self, deployment):
        """Why the deployment may not open a position now, or None."""
        if self.halt_path.exists():
            return f"the {HALT_FILE} file is present"
        halt = self.ledger.get_meta(f"global_halt_{deployment.mode}")
        if halt:
            return f"the {deployment.mode} deployments are halted: {halt['reason']}"
        cap = int(self.safeguards["max_orders_per_day"])
        if cap and deployment.mode == "live":
            day = int(self.now()) // 86_400 * 86_400
            if len(self.ledger.orders_since(day, "live")) >= cap:
                return f"the day's limit of {cap} live orders is reached"
        return None

    def reconcile(self, deployment, now):
        """Line the real position up with the replay's at a candle close."""
        status = self.status_of(deployment)
        if status == "NEEDS_REVIEW" or deployment.replay is None:
            return
        target = deployment.replay["state"]["position"]
        if deployment.in_position and target is None:
            last_exit = deployment.replay["last_exit"]
            self.sell(deployment, now, last_exit["reason"] if last_exit else "MODEL_EXIT")
        elif not deployment.in_position and target is not None and status == "ACTIVE":
            if target["time"] in (deployment.replay_entry, deployment.skipped_entry):
                return  # this entry was taken (and since stopped out) or skipped already
            why = self.stale_entry(deployment, target, now)
            if why:
                deployment.skipped_entry = target["time"]
                self.ledger.save_position(deployment.id, now, skipped_entry=target["time"])
                self.event("WARNING", f"skipped the model's entry: {why}; it waits for its next one", deployment.id)
                return
            self.buy(deployment, target, now)

    def stale_entry(self, deployment, target, now):
        """Why the replay's open position is too old or too far from the price to buy into, or None."""
        signal_close = target["time"] + deployment.seconds
        delay = max(self.safeguards["max_entry_delay_candles"], 0) * deployment.seconds + SETTLE_SECONDS + \
            2 * self.settings["poll_seconds"] + REFETCH_SECONDS
        if now - signal_close > delay:
            when = datetime.fromtimestamp(signal_close, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return f"it entered at {when}, too long ago to follow"
        quote = self.quotes.get(deployment.product_id)
        if quote is None or now - quote["time"] > self.safeguards["max_price_age_seconds"]:
            return None  # no fresh price: the buy itself waits for one
        deviation = quote["ask"] / target["price"] - 1
        if abs(deviation) > self.safeguards["max_entry_price_deviation"]:
            return (f"the price {quote['ask']:,.6g} is {deviation:+.2%} from the model's entry "
                    f"{target['price']:,.6g}")
        return None

    def check_exits(self, deployment, now):
        """Between candle closes: sell when the live price reaches a stop or the profit target the backtest
        would apply on this candle."""
        if not deployment.in_position or deployment.replay is None or deployment.replay["state"]["position"] is None:
            return
        if self.status_of(deployment) == "NEEDS_REVIEW":
            return
        quote = self.quotes.get(deployment.product_id)
        if quote is None or now - quote["time"] > self.safeguards["max_price_age_seconds"]:
            return
        state = deployment.replay["state"]
        triggered = [stop for stop in state["stops"] if quote["bid"] <= stop["price"]]
        if triggered:
            stop = max(triggered, key=lambda item: item["price"])
            self.sell(deployment, now, stop["reason"])
        elif state["profit_target"] and not state["profit_target"]["arms_trail"] and \
                quote["bid"] >= state["profit_target"]["price"]:
            self.sell(deployment, now, "PROFIT_LOCK")

    def check_global_drawdown(self, now):
        """Halt new entries (and, if set, sell everything) for a mode whose deployments together fell more than
        max_global_drawdown from their peak. Their combined value is chained into an index that moves only with
        returns, so adding or retiring a deployment doesn't look like a gain or a loss."""
        limit = self.safeguards["max_global_drawdown"]
        if not limit:
            return
        for mode in ("paper", "live"):
            members = sorted((d for d in self.deployments.values() if d.mode == mode), key=lambda d: d.id)
            if not members or any(self.quotes.get(d.product_id) is None and d.units for d in members):
                continue
            value = sum(d.equity(self.quotes.get(d.product_id, {}).get("mid")) for d in members)
            ids = [d.id for d in members]
            state = self.ledger.get_meta(f"global_index_{mode}") or {"index": 1.0, "peak": 1.0}
            if state.get("members") == ids and state.get("value"):
                state["index"] *= value / state["value"]
            state.update(members=ids, value=value, peak=max(state["peak"], state["index"]))
            self.ledger.set_meta(f"global_index_{mode}", state)
            fall = 1 - state["index"] / state["peak"] if state["peak"] > 0 else 0.0
            if fall >= limit and not self.ledger.get_meta(f"global_halt_{mode}"):
                reason = f"combined value fell {fall:.1%} from its peak, past the {limit:.0%} limit"
                self.ledger.set_meta(f"global_halt_{mode}", {"reason": reason, "time": int(now)})
                self.event("ERROR", f"{mode} deployments halted: {reason}")
                if self.safeguards["liquidate_on_global_drawdown"]:
                    for deployment in members:
                        if deployment.in_position:
                            self.sell(deployment, now, "GLOBAL_DRAWDOWN")

    # orders
    def broker_for(self, deployment):
        return self.live_broker if deployment.mode == "live" else self.paper_broker

    def _order_room(self, deployment, now, side):
        if now < deployment.retry_after:
            return False
        quote = self.quotes.get(deployment.product_id)
        if quote is None or now - quote["time"] > self.safeguards["max_price_age_seconds"]:
            self.event("WARNING", f"no fresh {deployment.product_id} price; the {side} waits", deployment.id)
            deployment.retry_after = now + ORDER_RETRY_SECONDS
            return False
        return True

    def _skip_entry(self, deployment, target, now, level, why):
        deployment.skipped_entry = target["time"]
        self.ledger.save_position(deployment.id, now, skipped_entry=target["time"])
        self.event(level, f"entry not taken: {why}", deployment.id)

    def buy(self, deployment, target, now):
        if not self._order_room(deployment, now, "buy"):
            return
        blocked = self.entries_blocked(deployment)
        if blocked:
            self._skip_entry(deployment, target, now, "WARNING", blocked)
            return
        info = self.market_data.product(deployment.product_id, now)
        if info.get("trading_disabled") or info.get("cancel_only") or info.get("status") not in (None, "online"):
            self.event("WARNING", f"{deployment.product_id} is not open for buying ({info.get('status')}); the "
                                  f"buy waits", deployment.id)
            deployment.retry_after = now + ORDER_RETRY_SECONDS
            return
        reserved, paid = tax_state(deployment, self.ledger.trades(deployment.id), now)
        spendable = max(deployment.cash - reserved - paid, 0.0)
        amount = spendable * float(deployment.replay["state"]["position_fraction"])
        cap = self.safeguards["max_order_value"]
        if cap and amount > cap:
            amount = cap
        if amount < max(info.get("quote_min_size") or 0.0, 1.0):
            self._skip_entry(deployment, target, now, "WARNING", f"{amount:,.2f} is below the smallest order")
            return
        if deployment.mode == "live":
            currency = self.settings["quote_currency"]
            available = self.live_broker.balances().get(currency, (0.0, 0.0))[0]
            if available < amount:
                self._skip_entry(deployment, target, now, "ERROR", f"the account has {available:,.2f} {currency} "
                                                                   f"free, short of the {amount:,.2f} entry")
                return
        placed = self._place(deployment, "BUY", now, quote_amount=amount, reason=target["reason"], info=info)
        if placed is not None:
            self.apply_buy(deployment, now, *placed, reason=target["reason"], replay_entry=target["time"])

    def apply_buy(self, deployment, now, client_order_id, fill, reason, replay_entry):
        """Add a buy's fill to the deployment and record it with its order, in one transaction."""
        cost = fill["value"] + fill["fees"]
        if fill["base"] > 0:
            if not deployment.in_position:
                deployment.entry_time, deployment.entry_price, deployment.entry_reason = int(now), fill["price"], reason
            deployment.cash -= cost
            deployment.units += fill["base"]
            deployment.cost_basis += cost
            deployment.fees_paid += fill["fees"]
            deployment.replay_entry = replay_entry
        self.ledger.settle_order(now, deployment.id, deployment.position_fields(), client_order_id,
                                 order_outcome(fill))
        self.export_reports()
        if fill["base"] > 0:
            self.event("INFO", f"bought {fill['base']:.8g} {deployment.base_currency} at {fill['price']:,.6g} for "
                               f"{cost:,.2f} ({deployment.mode}, {reason})", deployment.id)

    def sell(self, deployment, now, reason):
        if not self._order_room(deployment, now, "sell"):
            return
        info = self.market_data.product(deployment.product_id, now)
        units = deployment.units
        if deployment.mode == "live":
            try:
                available = self.live_broker.balances().get(deployment.base_currency, (0.0, 0.0))[0]
            except Exception as exc:  # sell anyway: the exchange refuses a sale of coins that aren't there
                self.event("WARNING", f"could not read balances before selling: {exc}", deployment.id)
                available = units
            if available < units * (1 - self.safeguards["balance_tolerance"]):
                self.flag_review(deployment, now, f"the account has {available:.8g} {deployment.base_currency} free "
                                                  f"but the ledger holds {units:.8g} for this deployment")
                units = min(units, available)  # still sell what is there: an exit must happen
        units = floor_to(units, info.get("base_increment"))
        if units <= 0 or units < self.dust_below(deployment, now):
            self.event("WARNING", f"{deployment.units:.8g} {deployment.base_currency} is below the smallest "
                                  f"order; left as dust and the position closed", deployment.id)
            deployment.entry_time = deployment.entry_price = deployment.entry_reason = None
            self.ledger.save_position(deployment.id, now, **deployment.position_fields())
            return
        placed = self._place(deployment, "SELL", now, base_amount=units, reason=reason, info=info)
        if placed is not None:
            self.apply_sell(deployment, now, *placed, reason=reason)

    def dust_below(self, deployment, now):
        """The amount of the coin too small to sell: under the exchange's minimum order or its size step. What's
        left of a position below it stays in the ledger as dust, and the position counts as closed."""
        info = self.market_data.product(deployment.product_id, now)
        return max(info.get("base_min_size") or 0.0, _float(info.get("base_increment")))

    def apply_sell(self, deployment, now, client_order_id, fill, reason):
        """Take a sell's fill from the deployment and record it with its order and the trade it closed, in one
        transaction. A part fill leaves the rest open, to sell at the next check."""
        sold = min(fill["base"], deployment.units)
        trade = None
        if sold > 0:
            share = sold / deployment.units
            cost = deployment.cost_basis * share
            proceeds = fill["value"] - fill["fees"]
            trade = {"deployment_id": deployment.id, "product_id": deployment.product_id, "mode": deployment.mode,
                     "entry_time": deployment.entry_time, "exit_time": int(now), "entry_price": deployment.entry_price,
                     "exit_price": fill["price"], "units": sold, "cost": cost, "proceeds": proceeds,
                     "fees": fill["fees"], "pnl": proceeds - cost, "entry_reason": deployment.entry_reason,
                     "exit_reason": reason}
            deployment.cash += proceeds
            deployment.units -= sold
            deployment.cost_basis -= cost
            deployment.fees_paid += fill["fees"]
            if deployment.units <= 1e-12 or deployment.units < self.dust_below(deployment, now):
                if deployment.units <= 1e-12:
                    deployment.units, deployment.cost_basis = 0.0, 0.0
                deployment.entry_time = deployment.entry_price = deployment.entry_reason = None
        self.ledger.settle_order(now, deployment.id, deployment.position_fields(), client_order_id,
                                 order_outcome(fill), trade)
        self.export_reports()
        if trade is not None:
            self.event("INFO", f"sold {sold:.8g} {deployment.base_currency} at {fill['price']:,.6g} for "
                               f"{trade['proceeds']:,.2f}, {trade['pnl']:+,.2f} ({deployment.mode}, {reason})",
                       deployment.id)

    def _place(self, deployment, side, now, reason, info, quote_amount=None, base_amount=None):
        """Record and send one order: (client order id, fill), or None when it failed or its outcome is unknown
        (the order's record then says which). The caller records the fill with the position it changes."""
        broker = self.broker_for(deployment)
        quote = self.quotes[deployment.product_id]
        client_order_id = str(uuid.uuid4())
        self.ledger.record_order(now, client_order_id=client_order_id, deployment_id=deployment.id,
                                 product_id=deployment.product_id, mode=deployment.mode, side=side, reason=reason,
                                 order_style=deployment.order_style, requested_quote=quote_amount,
                                 requested_base=base_amount, status="SUBMITTING")
        method = broker.buy if side == "BUY" else broker.sell
        amount = quote_amount if side == "BUY" else base_amount
        try:
            fill = method(deployment.product_id, amount, quote, client_order_id, deployment.order_style,
                          deployment.friction.slippage, deployment.friction.fill_fee_rate, info)
        except OrderUnknown as exc:
            self.ledger.update_order(client_order_id, now, status="UNKNOWN", error=str(exc),
                                     **({"exchange_order_id": exc.order_id} if exc.order_id else {}))
            self.flag_review(deployment, now, f"a {side.lower()} order's outcome is unknown: {exc}")
            return None
        except Exception as exc:
            self.ledger.update_order(client_order_id, now, status="REJECTED", error=str(exc))
            deployment.failures += 1
            deployment.retry_after = now + ORDER_RETRY_SECONDS
            self.event("ERROR", f"{side.lower()} order failed ({deployment.failures} in a row): {exc}", deployment.id)
            if deployment.failures >= MAX_ORDER_FAILURES:
                self.flag_review(deployment, now, f"{deployment.failures} orders failed in a row")
            return None
        deployment.failures = 0
        if fill["status"] != "FILLED":
            self.event("WARNING", f"{side.lower()} order {fill['status'].lower()}: {fill['base']:.8g} filled",
                       deployment.id)
            if fill["base"] <= 0:
                deployment.retry_after = now + ORDER_RETRY_SECONDS
        return client_order_id, fill

    def flag_review(self, deployment, now, why):
        deployment.status, deployment.review_note = "NEEDS_REVIEW", why
        self.ledger.save_position(deployment.id, now, status="NEEDS_REVIEW", review_note=why)
        self.event("ERROR", f"stopped for review: {why}. Check the exchange, then run "
                            f"`python live_trader.py --clear-review {deployment.id}`", deployment.id)

    def check_reviews(self):
        """Pick up reviews cleared from the command line while the trader runs."""
        for deployment in self.deployments.values():
            if deployment.status != "NEEDS_REVIEW":
                continue
            record = self.ledger.deployment(deployment.id)
            if record is None or record["status"] == "NEEDS_REVIEW":
                continue
            deployment.status, deployment.review_note = record["status"], record["review_note"]
            self.recover_orders()  # an order left unclear is settled before the deployment trades again
            if deployment.status == "NEEDS_REVIEW":
                continue
            deployment.load(self.ledger.deployment(deployment.id))
            deployment.failures, deployment.retry_after = 0, 0.0
            self.event("INFO", "review cleared; trading again", deployment.id)

    # recovery
    def recover_orders(self):
        """Settle live orders left unfinished by a crash, a restart or an unclear answer, from what the exchange
        says happened: a fill is added to its deployment as if it had just come back. An order the exchange
        doesn't list, or that is still unclear, stops its deployment for review."""
        now = self.now()
        unfinished = self.ledger.unfinished_orders()
        for order in unfinished:
            deployment = self.deployments.get(order["deployment_id"])
            if order["mode"] != "live":
                self.ledger.update_order(order["client_order_id"], now, status="ABANDONED",
                                         error="paper order unfinished at restart")
                continue
            exchange_id = order["exchange_order_id"] or self._find_exchange_order(order)
            if exchange_id is None:
                self.ledger.update_order(order["client_order_id"], now, status="NOT_FOUND")
                if deployment is not None:
                    self.flag_review(deployment, now, f"order {order['client_order_id']} was being sent when the "
                                                      f"trader stopped and the exchange doesn't list it")
                continue
            try:
                fill = self.live_broker.await_order(exchange_id)
            except OrderUnknown as exc:
                if deployment is not None:
                    self.flag_review(deployment, now, str(exc))
                continue
            if deployment is None:
                self.ledger.update_order(order["client_order_id"], now, exchange_order_id=exchange_id,
                                         **order_outcome(fill))
                continue
            self.event("WARNING", f"recovered order {exchange_id} ({order['side'].lower()}): {fill['base']:.8g} "
                                  f"filled", deployment.id)
            self.ledger.update_order(order["client_order_id"], now, exchange_order_id=exchange_id)
            if order["side"] == "BUY":
                self.apply_buy(deployment, now, order["client_order_id"], fill, order["reason"],
                               deployment.replay_entry)
            else:
                self.apply_sell(deployment, now, order["client_order_id"], fill, order["reason"])
        return len(unfinished)

    def _find_exchange_order(self, order):
        try:
            started = datetime.fromtimestamp(order["created_at"] - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            response = self.client.list_orders(product_ids=[order["product_id"]], start_date=started)
        except Exception as exc:
            LOGGER.warning("[LIVE] Could not list orders: %s", exc)
            return None
        for item in _field(response, "orders", default=[]) or []:
            if _field(item, "client_order_id") == order["client_order_id"]:
                return _field(item, "order_id")
        return None

    def check_balances(self):
        """For live deployments: does the exchange hold at least the coins the ledger says they hold?"""
        live = [d for d in self.deployments.values() if d.mode == "live" and d.units > 0]
        if not live:
            return
        try:
            balances = self.live_broker.balances()
        except Exception as exc:
            self.event("WARNING", f"could not read balances to check the ledger: {exc}")
            return
        held = {}
        for deployment in live:
            held[deployment.base_currency] = held.get(deployment.base_currency, 0.0) + deployment.units
        for currency, units in held.items():
            total = balances.get(currency, (0.0, 0.0))[1]
            if total < units * (1 - self.safeguards["balance_tolerance"]):
                for deployment in live:
                    if deployment.base_currency == currency and deployment.status != "NEEDS_REVIEW":
                        self.flag_review(deployment, self.now(), f"the account holds {total:.8g} {currency} but the "
                                                                 f"live deployments hold {units:.8g} between them")

    # reporting
    def snapshot(self):
        """Where everything stands, as plain data, for the dashboard (/api/live) and --status."""
        with self._snapshot_lock:
            return copy.deepcopy(self._snapshot)

    def publish(self):
        now = self.now()
        rows = []
        for deployment_id, why in self.blocked.items():
            rows.append({"id": deployment_id, "status": "BLOCKED", "note": why})
        for deployment in sorted(self.deployments.values(), key=lambda d: (d.product_id, d.id)):
            quote = self.quotes.get(deployment.product_id) or {}
            price = quote.get("mid")
            reserved, paid = tax_state(deployment, self.ledger.trades(deployment.id), now)
            equity = deployment.equity(price) if price else None
            replay = deployment.replay
            target = replay["state"]["position"] if replay else None
            rows.append({
                "id": deployment.id, "product_id": deployment.product_id, "mode": deployment.mode,
                "status": self.status_of(deployment), "review_note": deployment.review_note,
                "granularity": deployment.granularity, "order_style": deployment.order_style,
                "capital": deployment.capital, "cash": deployment.cash, "units": deployment.units,
                "price": price, "equity": equity, "return": None if equity is None else equity / deployment.capital - 1,
                "tax_reserved": reserved, "tax_paid": paid, "fees_paid": deployment.fees_paid,
                "position": None if not deployment.in_position else {
                    "since": deployment.entry_time, "price": deployment.entry_price,
                    "reason": deployment.entry_reason, "cost": deployment.cost_basis},
                "replay": None if replay is None else {
                    "in_position": target is not None, "equity": replay["equity"], "return": replay["return"],
                    "trades": replay["trade_count"], "last_candle": replay["last_candle"],
                    "stops": replay["state"]["stops"], "profit_target": replay["state"]["profit_target"],
                    "entry_signal": replay["state"]["entry_signal"], "paused": replay["state"]["paused"],
                    "halted": replay["state"]["halted"]},
                "in_sync": None if replay is None else (target is not None) == deployment.in_position,
                "started_at": deployment.started_at, "notes": deployment.notes + (
                    [f"replay failed: {deployment.replay_error}"] if deployment.replay_error else []) + (
                    ["waiting for its first candle to close"] if replay is None and not deployment.replay_error
                    else []),
            })
        snapshot = {
            "time": int(now), "state": self.state, "data_dir": str(self.paths.root), "mode": self.mode, "key": self.key, "records": str(self.paths.area),
            "system_live": self.system_live(), "halt_file": self.halt_path.exists(),
            "halts": {mode: self.ledger.get_meta(f"global_halt_{mode}") for mode in ("paper", "live")},
            "fees": None if not self.fee_inputs else self.fee_inputs.get("exchange_fees"),
            "currencies": sorted({row["product_id"] for row in rows if row.get("product_id")}),
            "deployments": rows, "events": self.ledger.events(20),
            "recovery": {**{key: value for key, value in (getattr(self, "recovery", None) or {}).items()
                            if key != "previous"},
                         "last_backup": self.ledger.get_meta("last_backup"),
                         "outages": (self.ledger.get_meta("outages") or [])[-5:]},
        }
        with self._snapshot_lock:
            self._snapshot = snapshot


def describe_snapshot(snapshot):
    """The snapshot as text for the terminal."""
    lines = [f"Live trader at {datetime.fromtimestamp(snapshot['time'], timezone.utc):%Y-%m-%d %H:%M:%S UTC}: "
             f"{'LIVE TRADING (real orders)' if snapshot.get('mode') == 'live' else 'PAPER TRADING'}"
             f"{' because ' + snapshot['key']['reason'] if snapshot.get('key') else ''}"
             f"{'; HALT file present: no new entries' if snapshot['halt_file'] else ''}"]
    for mode, halt in snapshot["halts"].items():
        if halt:
            lines.append(f"  {mode} deployments halted: {halt['reason']}")
    lines.append(f"  {'deployment':24} {'market':10} {'mode':5} {'status':13} {'value':>12} {'return':>8} "
                 f"{'position':9} {'model':9}")
    for row in snapshot["deployments"]:
        if row["status"] == "BLOCKED":
            lines.append(f"  {row['id']:24} BLOCKED: {row['note']}")
            continue
        value = "-" if row["equity"] is None else f"{row['equity']:,.2f}"
        change = "-" if row["return"] is None else f"{row['return']:+.2%}"
        model = "-" if row["replay"] is None else ("in" if row["replay"]["in_position"] else "out")
        lines.append(f"  {row['id']:24} {row['product_id']:10} {row['mode']:5} {row['status']:13} {value:>12} "
                     f"{change:>8} {'held' if row['position'] else 'cash':9} {model:9}")
        for note in row["notes"] + ([row["review_note"]] if row["review_note"] else []):
            lines.append(f"      note: {note}")
    return "\n".join(lines)


# --- the command line -------------------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(
        description="Trade model deployments live or on paper, on its own on Windows or Unix (see "
                    "Project_Documentation/live_trading.md). Your data lives in a data folder outside the program. "
                    "RISK WARNING: a speculative automated trader; you can lose your entire investment.")
    run = parser.add_argument_group("running it")
    run.add_argument("--background", action="store_true",
                     help="start it detached from this terminal, restarted if it crashes, and return at once")
    run.add_argument("--stop", action="store_true", help="stop the running trader cleanly and wait for it")
    run.add_argument("--status", action="store_true",
                     help="whether it is running, its web portal, its data folder and the deployments")
    run.add_argument("--headless", action="store_true",
                     help="run in this terminal but unattended: never ask questions, log to a file")
    run.add_argument("--once", action="store_true", help="run one cycle, print the status and exit")
    run.add_argument("--paper", action="store_true",
                     help="paper-trade even if the API key can trade (a view-only key always paper-trades)")
    run.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)  # what --background starts
    data = parser.add_argument_group("your data (kept outside the program)")
    data.add_argument("--data-dir", help=f"the data folder for this command (default: {data_home.ENV_VAR}, else "
                                         f"the folder set with --set-data-dir, else {data_home.default_data_dir()})")
    data.add_argument("--set-data-dir", metavar="FOLDER",
                      help="use FOLDER as the data folder from now on: moves the current data there, or picks up "
                           "the data already in it (after reinstalling the program)")
    data.add_argument("--init", action="store_true",
                      help="first-time set-up: put the settings template in the data folder and show where")
    data.add_argument("--mode", choices=data_home.MODES,
                      help="whose records --status, --backup, --clear-review and --clear-halt use, when both live "
                           "and paper records exist (trading itself takes its mode from the API key)")
    data.add_argument("--config", help="the settings file (default: <data folder>/live_config/live_trading.json)")
    data.add_argument("--ledger", help="the trading ledger (default: <data folder>/ledger/trading_ledger.db)")
    data.add_argument("--log-file", help="log file (default when headless or in the background: <data "
                                         "folder>/logs/live_trader.log)")
    data.add_argument("--env-file", help="the live trader's own configuration: its API key, portal and paper switch "
                                         "(default: <data folder>/live_trader.env; never the simulator's .env)")
    data.add_argument("--base-dir", default=str(ROOT),
                      help="the program folder, where relative profile paths in the settings start from")
    net = parser.add_argument_group("the web portal")
    net.add_argument("--no-dashboard", action="store_true", help="don't serve the dashboard and web portal")
    net.add_argument("--host", help="address the web portal listens on (default: DASHBOARD_HOST in .env, "
                                    "0.0.0.0: every interface, so other machines on the network can open it)")
    net.add_argument("--port", type=int, help="web portal port (default: LAN_PORT in .env)")
    fix = parser.add_argument_group("after a problem")
    fix.add_argument("--clear-review", metavar="ID", help="let a deployment stopped for review trade again")
    fix.add_argument("--clear-halt", choices=("paper", "live"), help="lift a combined-drawdown halt")
    keep = parser.add_argument_group("backups, outages and moving to another machine")
    keep.add_argument("--backup", nargs="?", const="", metavar="FILE",
                      help="write a recovery bundle now (default: <data folder>/backups/live_trader_backup_<time>.zip)")
    keep.add_argument("--with-candles", action="store_true",
                      help="with --backup: include the price history, so a new machine needn't download it")
    keep.add_argument("--restore", metavar="FILE", help="put a recovery bundle in place (the trader must be "
                                                        "stopped); files it replaces are kept in backups/")
    return parser


ENV_TEMPLATE = ROOT / "live_config" / ".example" / "live_trader.env.example"


def simulator_env_files(args):
    """The configuration files the simulator reads (.env in the program folder and in the folder commands run
    from), which the live trader must never use: they hold the simulator's key, and the key decides whether the
    live trader trades real money."""
    files = {(Path(args.base_dir) / ".env").resolve(), Path(".env").resolve()}
    return {path for path in files if path.parent != data_home.resolve_data_dir(args.data_dir)[0]}


def check_env_file(where):
    """None if the live trader's configuration file can be used, else why not."""
    if where.simulator_env:
        return (f"{where.env_file} is the simulator's configuration file. The live trader uses its own, so the "
                f"simulator's key never decides whether it trades real money: {where.paths.env}")
    if not where.env_file.exists():
        return (f"The live trader's configuration file is missing: {where.env_file}\n"
                f"Create it with `python live_trader.py --init`, then put in it the API key the live trader should "
                f"use (view-only: paper trading; with Trade: live trading).")
    return None


class Locations:
    """Where this command's files are: the data folder and anything given on the command line instead."""

    def __init__(self, args):
        root, self.chosen_by = data_home.resolve_data_dir(args.data_dir)
        self.paths = data_home.DataPaths(root)
        self.config = Path(args.config).resolve() if args.config else self.paths.settings
        self.ledger_override = Path(args.ledger).resolve() if args.ledger else None
        # The live trader's own configuration file: never the simulator's .env (see simulator_env_files).
        self.env_file = Path(args.env_file).resolve() if args.env_file else self.paths.env
        self.simulator_env = self.env_file in simulator_env_files(args)
        self.run = self.paths.run

    def ledger(self, mode):
        return self.ledger_override or self.paths.for_mode(mode).ledger

    def modes(self, args):
        """The modes a bookkeeping command acts on: --mode, else every mode with records."""
        if args.mode:
            return [args.mode]
        if self.ledger_override:
            modes = data_home.ledger_modes(self.ledger_override) or {"paper"}
            return sorted(modes)
        return self.paths.modes_with_records()


def _passthrough(args, where):
    """The settings a background trader is started with, as arguments, all absolute (it may run from another
    folder)."""
    passed = ["--data-dir", str(where.paths.root), "--base-dir", str(Path(args.base_dir).resolve()),
              "--env-file", str(where.env_file)]
    for flag, value in (("--config", args.config), ("--ledger", args.ledger), ("--log-file", args.log_file)):
        if value:
            passed += [flag, str(Path(value).resolve())]
    if args.host:
        passed += ["--host", args.host]
    if args.port:
        passed += ["--port", str(args.port)]
    if args.no_dashboard:
        passed.append("--no-dashboard")
    if args.paper:
        passed.append("--paper")
    return passed


def main(argv=None):
    import process_control as pc
    args = _parser().parse_args(argv)
    safe_console()
    where = Locations(args)
    if args.set_data_dir:
        return _set_data_dir(args, where)
    where.paths.create()
    _adopt_program_folder_data(args, where)
    if args.init:
        return _init(where)
    script = str(Path(__file__).resolve())

    if args.stop:
        if not pc.status(where.run)["running"]:
            pc.clear_stop(where.run)
            print("The live trader is not running.")
            return 0
        pc.request_stop(where.run)
        print("Asked the live trader to stop; waiting for it to finish its cycle...")
        if pc.wait_for_stop(where.run, timeout=180):
            print("The live trader has stopped.")
            return 0
        print(f"It hasn't stopped yet; check --status, and the log in {where.paths.logs}.")
        return 1
    if args.background:
        print(DISCLAIMER + "\n")
        command = [sys.executable, script, "--supervise", *_passthrough(args, where)]
        try:
            state = pc.start_background(command, where.run, cwd=os.getcwd())
        except pc.AlreadyRunning as exc:
            print(exc)
            return pc.EXIT_ALREADY_RUNNING
        if not state["running"]:
            print(f"The live trader didn't start; see {where.paths.logs} for why.")
            return 1
        print("The live trader is running in the background.\n" + pc.describe_status(state))
        print(f"Data folder: {where.paths.root}\nLog: {Path(args.log_file or where.paths.log_file).resolve()}\n"
              f"Stop it with: python live_trader.py --stop")
        return 0
    if args.supervise:
        return _supervise(args, where, script)
    if args.restore:
        return _restore(args, where)
    if args.backup is not None:
        return _backup(args, where)
    if args.status:
        return _status(args, where)
    if args.clear_review or args.clear_halt:
        return _clear(args, where)
    return _trade(args, where)


def _open_ledger(where, mode):
    """The mode's trading ledger, or (None, exit code) after saying why it can't be used."""
    import process_control as pc
    path = where.ledger(mode)
    problem = recovery.integrity_problem(path)
    if problem:
        latest = recovery.latest_bundle(where.paths.for_mode(mode).backups)
        print(f"The {mode} trading ledger at {path} is damaged: {problem}\n"
              f"Nothing will trade on it. Restore a backup with:\n  python live_trader.py --restore "
              f"{latest or '<backup .zip>'}")
        LOGGER.error("[LIVE] The %s ledger is damaged: %s", mode, problem)
        return None, pc.EXIT_LEDGER
    return TradingLedger(path), None


def _status(args, where):
    import process_control as pc
    print(pc.describe_status(pc.status(where.run)))
    worker = pc.status(where.run)["worker"] or {}
    if worker.get("mode"):
        print(f"Trading mode: {worker['mode'].upper()} ({worker.get('reason', '')})")
    print(f"Data folder: {where.paths.root} ({where.chosen_by})")
    print(f"Configuration: {where.env_file}" + (" (missing: run --init)" if not where.env_file.exists() else ""))
    modes = where.modes(args)
    if not modes:
        print("No trading records yet.")
    for mode in modes:
        ledger, code = _open_ledger(where, mode)
        if ledger is None:
            return code
        print(f"\n{mode.upper()} records: {where.ledger(mode).parent.parent}")
        print(describe_recovery(ledger))
        for row in ledger.deployments():
            position = f"{row['units']:.8g} held since {row['entry_time']}" if row["entry_time"] else "cash"
            print(f"  {row['id']:24} {row['product_id']:10} {row['status']:13} cash "
                  f"{row['cash']:,.2f}, {position}{'; ' + row['review_note'] if row['review_note'] else ''}")
        ledger.close()
    return 0


def _clear(args, where):
    """--clear-review ID (in whichever mode's records hold it) and --clear-halt MODE."""
    if args.clear_halt:
        ledger, code = _open_ledger(where, args.clear_halt)
        if ledger is None:
            return code
        ledger.set_meta(f"global_halt_{args.clear_halt}", None)
        ledger.set_meta(f"global_index_{args.clear_halt}", None)  # measure the drawdown afresh from here
        ledger.log_event(time.time(), "INFO", f"{args.clear_halt} halt cleared by the operator")
        print(f"The {args.clear_halt} halt is lifted.")
        return 0
    holders = []
    for mode in where.modes(args):
        ledger, code = _open_ledger(where, mode)
        if ledger is None:
            return code
        if ledger.deployment(args.clear_review) is not None:
            holders.append((mode, ledger))
        else:
            ledger.close()
    if not holders:
        _parser().error(f"No deployment named {args.clear_review!r} in the trading records")
    if len(holders) > 1:
        _parser().error(f"Both the live and the paper records have {args.clear_review!r}; say which with --mode")
    mode, ledger = holders[0]
    ledger.save_position(args.clear_review, time.time(), status="ACTIVE", review_note=None)
    ledger.log_event(time.time(), "INFO", "review cleared by the operator", args.clear_review)
    print(f"{args.clear_review} ({mode}) may trade again.")
    return 0


TEMPLATE = ROOT / "live_config" / ".example" / "live_trading.example.json"


def _init(where):
    """Put the templates for the live trader's settings and its own configuration file in the data folder,
    leaving any that are already there."""
    print(f"Data folder: {where.paths.root} ({where.chosen_by})")
    for path, template, what in ((where.config, TEMPLATE, "Settings (your deployments)"),
                                 (where.paths.env, ENV_TEMPLATE, "Configuration (the live trader's API key)")):
        if path.exists():
            print(f"{what} already exist: {path} (left as it is)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, path)
        print(f"{what} template written to: {path}")
    print("Edit them there: list your deployments, and put in the API key the live trader should use (view-only: "
          "paper trading; with Trade: live trading).\nThe simulator's .env is not used by the live trader. Then "
          "run: python live_trader.py --once")
    return 0


def _set_data_dir(args, where):
    """Use another data folder from now on: move the data there, or pick up the data already in it."""
    import process_control as pc
    new = Path(args.set_data_dir).expanduser().resolve()
    current = where.paths.root
    if pc.status(where.run)["running"] or pc.status(data_home.DataPaths(new).run)["running"]:
        print("Stop the live trader first (python live_trader.py --stop), then change its data folder.")
        return pc.EXIT_ALREADY_RUNNING
    if new != current and data_home.DataPaths(new).has_data():
        data_home.remember(new)
        print(f"Using the trading data already in {new}.")
        if data_home.DataPaths(current).has_data():
            print(f"The data in {current} was left where it is.")
    elif new != current and data_home.DataPaths(current).has_data():
        try:
            moved = data_home.move_contents(current, new)
        except (OSError, ValueError) as exc:
            print(f"Couldn't move the data: {exc}")
            return 1
        data_home.remember(new)
        print(f"Moved {', '.join(moved)} from {current} to {new}; it is the data folder from now on.")
    else:
        data_home.remember(new)
        print(f"{new} is the data folder from now on.")
    data_home.DataPaths(new).create()
    if os.environ.get(data_home.ENV_VAR):
        print(f"Note: the {data_home.ENV_VAR} environment variable is set and takes precedence.")
    return 0


def _adopt_program_folder_data(args, where):
    """Once: move live trading data an earlier version kept inside the program folder into the data folder."""
    import process_control as pc
    program = Path(args.base_dir)
    if where.paths.root.resolve() == program.resolve() or pc.status(program / "run")["running"]:
        return
    moved = data_home.adopt_program_folder_data(where.paths, program)
    if moved:
        print(f"Moved the live trading data from the program folder to the data folder {where.paths.root}:")
        for path in moved:
            print(f"  {path}")


def _active_tax_profile(where):
    """Your tax profile, kept in the data folder. One found only in the program folder is copied in first, so it
    survives the program; taxes.py then reads the data folder's copy."""
    import taxes
    if not where.paths.tax_profile.exists() and taxes.PROFILE_FILE.exists():
        where.paths.tax_profile.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(taxes.PROFILE_FILE, where.paths.tax_profile)
        LOGGER.info("[LIVE] Copied your tax profile into the data folder: %s", where.paths.tax_profile)
    if where.paths.tax_profile.exists():
        taxes.PROFILE_FILE = where.paths.tax_profile
    return taxes.PROFILE_FILE if taxes.PROFILE_FILE.exists() else None


def describe_recovery(ledger):
    """The ledger's record of the last run, outages and backups, as text."""
    lines = []
    session = ledger.get_meta("session")
    if session:
        state = "stopped cleanly" if session.get("clean") else "did not stop cleanly (or is running)"
        lines.append(f"Last run: started {recovery._when(session['started'])} on {session.get('host')}, last seen "
                     f"{recovery._when(session['heartbeat'])}, {state}")
    outages = ledger.get_meta("outages") or []
    if outages:
        last = outages[-1]
        lines.append(f"Last outage: offline {recovery.describe_seconds(last['seconds'])} until "
                     f"{recovery._when(last['resumed'])} ({len(outages)} recorded)")
    backup = ledger.get_meta("last_backup")
    lines.append(f"Last backup: {recovery._when(backup['time'])}, {backup['path']}" if backup else
                 "Last backup: none yet")
    return "\n".join(lines)


def _backup(args, where):
    """A recovery bundle of each mode's records (or the one --mode names)."""
    now = time.time()
    modes = where.modes(args)
    if not modes:
        print("There are no trading records to back up yet.")
        return 1
    if args.backup and len(modes) > 1:
        print("Both live and paper records exist; say which to write to that file with --mode")
        return 1
    for mode in modes:
        destination = Path(args.backup) if args.backup else (
            where.paths.for_mode(mode).backups / f"{recovery.BUNDLE_PREFIX}{recovery._stamp(now)}.zip")
        try:
            manifest = recovery.write_bundle(destination, args.base_dir, where.ledger(mode), where.config, now,
                                             include_candles=args.with_candles, cache_dir=where.paths.cache,
                                             tax_profile=_active_tax_profile(where), mode=mode)
        except recovery.RecoveryError as exc:
            print(exc)
            return 1
        print(f"{mode.upper()} recovery bundle written: {destination.resolve()}\n"
              f"  {len(manifest['deployments'])} deployments; holds " + ", ".join(manifest["contents"]))
    print("Bundles never hold live_trader.env: copy the live trader's API key to a new machine yourself.")
    return 0


def _restore(args, where):
    import process_control as pc
    if pc.status(where.run)["running"]:
        print("Stop the live trader first (python live_trader.py --stop), then restore.")
        return pc.EXIT_ALREADY_RUNNING
    try:
        mode = recovery.bundle_mode(args.restore)
        paths = where.paths.for_mode(mode)
        with pc.InstanceLock(where.run / pc.LOCK_NAME):
            manifest, restored, replaced = recovery.restore_bundle(args.restore, paths, time.time(),
                                                                   where.ledger(mode), where.config)
    except (pc.AlreadyRunning, recovery.RecoveryError) as exc:
        print(exc)
        return 1
    print(f"Restored the {mode.upper()} records made {manifest['created_utc']} on {manifest['host']} into "
          f"{paths.area}:")
    for path in restored:
        print(f"  {path}")
    if replaced:
        print(f"Files it replaced were moved to {replaced}")
    key = "an API key that can trade" if mode == "live" else "a view-only API key (or --paper)"
    print(f"Next: make sure {where.env_file} holds {key} (keys are never in a bundle; `--init` creates the "
          f"file), then start the trader:\n"
          "  python live_trader.py --background\n"
          "It replays every model from when it was deployed and carries on: exits missed while it was offline are "
          "sold, and entries missed are skipped.")
    return 0


def _trade(args, where):
    """Trade in this process (in the terminal, headless, or as the background supervisor's worker): check what the
    API key may do (waiting for the network if need be), then trade live or on paper with that mode's records."""
    import process_control as pc
    log_file = args.log_file or (where.paths.log_file if args.headless else None)
    if log_file:
        log_to_file(log_file)
    print(DISCLAIMER + "\n")
    for line in DISCLAIMER.splitlines():
        LOGGER.warning("[LIVE] %s", line)
    lock = pc.InstanceLock(where.run / pc.LOCK_NAME)
    try:
        lock.acquire()
    except pc.AlreadyRunning as exc:
        LOGGER.error("[LIVE] %s", exc)
        print(exc)
        return pc.EXIT_ALREADY_RUNNING
    engine = ledger = None
    stop = threading.Event()
    stop_on_signals(stop)
    pc.watch_for_stop(where.run, stop)
    try:
        from engine import QuantitativeTradingEngine, load_environment_config
        try:
            config = load_live_config(where.config)
        except LiveConfigError as exc:
            LOGGER.error("[LIVE] %s", exc)
            print(exc)
            return pc.EXIT_CONFIG
        problem = check_env_file(where)
        if problem:
            LOGGER.error("[LIVE] %s", problem)
            print(problem)
            return pc.EXIT_CONFIG
        LOGGER.info("[LIVE] Data folder: %s; configuration: %s", where.paths.root, where.env_file)
        _active_tax_profile(where)
        _seed_price_cache(args, where)
        env = load_environment_config(str(where.env_file))
        engine = QuantitativeTradingEngine(base_directory=args.base_dir, env_file=str(where.env_file))
        key_file = env["coinbase"].get("key_file")
        if key_file and not Path(key_file).is_absolute():  # a key file named in it sits beside it, in the data folder
            engine.api_config["coinbase"]["key_file"] = str(where.env_file.parent / key_file)
        # Unattended, nothing may wait for an answer: an SDK install or PATH change is declined (and logged).
        decline = (lambda: False) if args.headless else None
        client = engine.create_coinbase_client(dependency_prompt=decline, path_prompt=decline)
        force_paper = args.paper or bool(env.get("force_paper_trading"))
        try:
            checked = determine_mode(client, force_paper, None if args.once else stop)
        except (NoNetwork, LiveConfigError) as exc:
            LOGGER.error("[LIVE] %s", exc)
            print(f"Can't check the API key, so nothing will trade: {exc}")
            return pc.EXIT_CONFIG if isinstance(exc, LiveConfigError) else 1
        if checked is None:
            return pc.EXIT_OK  # stopped while waiting for the network
        mode, key = checked
        LOGGER.warning("[LIVE] %s: %s", "LIVE TRADING with real orders" if mode == "live" else "PAPER TRADING",
                       key["reason"])
        print(f"{'LIVE TRADING with real orders' if mode == 'live' else 'PAPER TRADING'}: {key['reason']}.")
        if key["can_transfer"]:
            LOGGER.warning("[LIVE] This API key can also transfer funds out; the trader never needs that. Use a key "
                           "with View and Trade only.")
        ledger, code = _open_ledger(where, mode)
        if ledger is None:
            return code
        trader = LiveTrader(config, client, ledger, env, base_dir=args.base_dir, cache_dir=where.paths.cache,
                            data_dir=where.paths.root, mode=mode, key=key)
        try:
            ledger.claim_mode(mode)
        except LedgerModeError as exc:
            LOGGER.error("[LIVE] %s", exc)
            print(exc)
            return pc.EXIT_CONFIG
        engine.live_trader = trader
        if args.once:
            trader.start()
            trader.end_session()
            print(describe_snapshot(trader.snapshot()))
            return pc.EXIT_OK
        urls = []
        if not args.no_dashboard:
            urls = engine.spin_up_lan_isolated_dashboard(args.port or env.get("lan_port", 8080), host=args.host)
            if urls:
                print("Web portal: " + ", ".join(url + "portal" for url in urls))
        pc.write_record(where.run, pc.WORKER_FILE, urls=urls, log=str(log_file) if log_file else None,
                        data_dir=str(where.paths.root), mode=mode, reason=key["reason"])
        LOGGER.info("[LIVE] Trading; stop with Ctrl+C or `python live_trader.py --stop`")
        try:
            trader.run(stop)
        except KeyboardInterrupt:
            stop.set()
        LOGGER.info("[LIVE] Stopped")
        if not args.headless:
            print(describe_snapshot(trader.snapshot()))
        return pc.EXIT_OK
    finally:
        if engine is not None and engine.web_server is not None:
            engine.web_server.shutdown()
        pc.remove_record(where.run, pc.WORKER_FILE)
        if ledger is not None:
            ledger.close()
        lock.release()


def _seed_price_cache(args, where):
    """The trader keeps its own price history in the data folder. The first time, start it from the program
    folder's (the simulator's) instead of downloading years of candles."""
    target = where.paths.cache / "candles.db"
    source = Path(args.base_dir) / "data" / "candles.db"
    if target.exists() or not source.exists():
        return
    try:
        recovery.snapshot_ledger(source, target)
        LOGGER.info("[LIVE] Started the price history in the data folder from %s", source)
    except Exception as exc:  # it is only a head start: the trader downloads what it needs
        LOGGER.warning("[LIVE] Could not copy the price history (%s); it will be downloaded", exc)
        target.unlink(missing_ok=True)


def _supervise(args, where, script):
    """The background supervisor: keep a headless trader running until --stop, restarting it after a crash."""
    import subprocess

    import process_control as pc
    log_to_file(where.paths.supervisor_log)
    supervisor_log = logging.getLogger("quant_trading_bot.supervisor")
    pc.write_record(where.run, pc.SUPERVISOR_FILE, command=" ".join(sys.argv[1:]))
    # A signal to the supervisor (SIGTERM, Ctrl+Break) stops the trader too, through the stop file.
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, lambda *_: pc.request_stop(where.run))
            except (ValueError, OSError):
                pass
    command = [sys.executable, script, "--headless", *_passthrough(args, where)]

    def launch():
        return subprocess.Popen(command, cwd=os.getcwd(), **pc.hidden_options())

    supervisor_log.info("[SUPERVISOR] Keeping the live trader running (process %s); data folder %s", os.getpid(),
                        where.paths.root)
    try:
        return pc.supervise(launch, where.run, lambda message: supervisor_log.info("[SUPERVISOR] %s", message))
    finally:
        supervisor_log.info("[SUPERVISOR] Stopped")
        pc.remove_record(where.run, pc.SUPERVISOR_FILE)
        pc.clear_stop(where.run)


def safe_console():
    """Never let printing stop the trader: a Windows console or a redirected stream that can't show a character
    (an emoji in a log line) shows an escape for it instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # pragma: no cover - a stream that can't be reconfigured
                pass


def log_to_file(path, max_bytes=5_000_000, backups=5):
    """Also write every log line to `path` (UTF-8), keeping it to `backups` rotated files of `max_bytes`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return handler


def stop_on_signals(stop):
    """Set `stop` when the system asks the trader to stop, on Unix or Windows: SIGTERM (kill, docker),
    SIGHUP (the terminal closing), SIGINT (Ctrl+C) and, on Windows, SIGBREAK (Ctrl+Break, and console close)."""
    def handler(number, frame):
        LOGGER.info("[LIVE] Signal %s received; stopping after the current cycle", number)
        stop.set()

    names = ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK")
    installed = []
    for name in names:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
            installed.append(name)
        except (ValueError, OSError):  # not the main thread, or not supported here
            pass
    return installed


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic market scenarios: test models on market activity history hasn't shown.

A scenario is a small dict, kept in scenario sets under scenarios/ (see scenarios/README.md):

    {"name": "Crash -50% in a week", "type": "crash", "placement": "append",
     "drop": 0.5, "days": 7, "recovery_days": 180, "recovery": 0.5, "volatility": 1.0, "seed": 1}

Types (every move is in log terms, with realistic noise on top):
    crash       falls `drop` over `days`, then regains `recovery` of the fall over `recovery_days`
    bear        grinds down `drop` over `days`
    pump        rises `rise` over `days`, then falls `drop` over `collapse_days`
    sideways    swings within about `range` either side of the start for `days`
    volatility  moves like the market's own history, `volatility` times as wild, for `days`
    resample    `paths` random paths of `days`, stitched from blocks of `block_days` of real history

Placement:
    append      after the last real candle, continuing from its price
    at          from the date `at`: the scenario replaces history from there for its length,
                and later history is shifted to continue from where the scenario ended

Chains: a scenario with "chain": true continues the run of the scenario above it in the set, so
modules can be strung together (a crash, then chop, then a calm rally) into one path; the run's
placement is its first module's. A run without chained modules is a single scenario.

Realism: each phase has a shaped trend (crashes fall fast and slow down, pumps accelerate into
the top, recoveries ease in) and wanders around it like the market does, with fat-tailed moves
and calm and violent stretches, pinned to end where the scenario says.

Synthetic candles are built from the market's own history at the model's candle size: noise
uses its typical move per candle, highs and lows its typical range, volume its median. The
same scenario and seed always give the same candles. Each scenario is measured from its first
candle (`start_time`), with indicators warmed up on the real history before it.
"""

import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCENARIO_DIR = Path(__file__).parent / "scenarios"
DEFAULT_SET = SCENARIO_DIR / "stress_test.json"
TYPES = ["crash", "bear", "pump", "sideways", "volatility", "resample"]
# Parameters each type uses, with defaults; every scenario also has name, type, placement, at and seed.
PARAMETERS = {
    "crash": {"drop": 0.5, "days": 7, "recovery_days": 180, "recovery": 0.5, "volatility": 1.0},
    "bear": {"drop": 0.7, "days": 365, "volatility": 1.0},
    "pump": {"rise": 2.0, "days": 60, "drop": 0.6, "collapse_days": 30, "volatility": 1.0},
    "sideways": {"range": 0.15, "days": 180, "volatility": 1.0},
    "volatility": {"days": 90, "volatility": 2.0},
    "resample": {"days": 365, "paths": 20, "block_days": 7},
}
DEFAULT_SCENARIOS = [
    {"name": "Crash -50% in a week, half recovered in 6 months", "type": "crash", "placement": "append",
     "drop": 0.5, "days": 7, "recovery_days": 180, "recovery": 0.5, "volatility": 1.0, "seed": 1},
    {"name": "Bear market -70% over a year", "type": "bear", "placement": "append", "drop": 0.7, "days": 365,
     "volatility": 1.0, "seed": 2},
    {"name": "Pump +200% in 2 months, then -60% in a month", "type": "pump", "placement": "append", "rise": 2.0,
     "days": 60, "drop": 0.6, "collapse_days": 30, "volatility": 1.0, "seed": 3},
    {"name": "Calm rally +100% over 4 months, then -30%", "type": "pump", "placement": "append", "rise": 1.0,
     "days": 120, "drop": 0.3, "collapse_days": 30, "volatility": 0.3, "seed": 7},
    {"name": "Sideways chop, about 15% either way, for 6 months", "type": "sideways", "placement": "append",
     "range": 0.15, "days": 180, "volatility": 1.0, "seed": 4},
    {"name": "Volatility doubles for 3 months", "type": "volatility", "placement": "append", "days": 90,
     "volatility": 2.0, "seed": 5},
    {"name": "Resampled history, 1 year, 20 paths", "type": "resample", "placement": "append", "days": 365,
     "paths": 20, "block_days": 7, "seed": 6},
    # A chained run: each module continues from where the one above ended.
    {"name": "Crash -40%", "type": "crash", "placement": "append", "drop": 0.4, "days": 5, "recovery_days": 30,
     "recovery": 0.2, "volatility": 1.0, "seed": 8},
    {"name": "Chop for 3 months", "type": "sideways", "chain": True, "range": 0.1, "days": 90, "volatility": 1.0,
     "seed": 9},
    {"name": "Calm rally +80%", "type": "pump", "chain": True, "rise": 0.8, "days": 120, "drop": 0.2,
     "collapse_days": 20, "volatility": 0.4, "seed": 10},
]


class ScenarioError(ValueError):
    pass


def normalize(scenario):
    """The scenario with every parameter of its type filled in (defaults for those missing); raises
    ScenarioError for an unknown type, a bad placement or out-of-range values."""
    kind = scenario.get("type")
    if kind not in PARAMETERS:
        raise ScenarioError(f"{scenario.get('name', 'scenario')}: type must be one of {', '.join(TYPES)}")
    out = {"name": scenario.get("name") or kind, "type": kind, "placement": scenario.get("placement", "append"),
           "at": scenario.get("at", ""), "seed": int(scenario.get("seed", 1)), "chain": bool(scenario.get("chain"))}
    for key, default in PARAMETERS[kind].items():
        try:
            out[key] = float(scenario.get(key, default))
        except (TypeError, ValueError):
            raise ScenarioError(f"{out['name']}: {key} must be a number") from None
    problems = []
    if out["placement"] not in ("append", "at"):
        problems.append('placement must be "append" or "at"')
    if out["placement"] == "at":
        try:
            datetime.strptime(str(out["at"]), "%Y-%m-%d")
        except ValueError:
            problems.append("at must be a date such as 2024-03-01")
    for key in ("days", "recovery_days", "collapse_days", "block_days"):
        if key in out and out[key] <= 0:
            problems.append(f"{key} must be above 0")
    if "drop" in out and not 0 < out["drop"] < 1:
        problems.append("drop must be between 0 and 1, such as 0.5 for -50%")
    if "recovery" in out and not 0 <= out["recovery"] <= 1:
        problems.append("recovery must be between 0 and 1")
    if "volatility" in out and out["volatility"] < 0:
        problems.append("volatility can't be negative")
    if "paths" in out:
        out["paths"] = int(out["paths"])
        if not 1 <= out["paths"] <= 500:
            problems.append("paths must be from 1 to 500")
    if problems:
        raise ScenarioError(f"{out['name']}: " + "; ".join(problems))
    return out


def load_set(path=DEFAULT_SET):
    """The scenarios of a scenario set file; the default set is written the first time it is asked for."""
    path = Path(path)
    if not path.exists() and path == DEFAULT_SET:
        save_set(DEFAULT_SCENARIOS, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", data) if isinstance(data, dict) else data
    return [normalize(scenario) for scenario in scenarios]


def save_set(scenarios, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scenarios": [normalize(s) for s in scenarios]}, indent=2) + "\n", encoding="utf-8")


def scenario_sets(folder=SCENARIO_DIR):
    load_set(DEFAULT_SET) if folder == SCENARIO_DIR else None
    return sorted(Path(folder).glob("*.json"))


# --- building candles -------------------------------------------------------------------

def _profile(candles):
    """The market's typical behaviour per candle: log-return spread, high-low range, volume."""
    close = np.array([c["close"] for c in candles], dtype=float)
    returns = np.diff(np.log(close))
    spread = float(np.std(returns[-min(len(returns), 2000):])) if len(returns) > 1 else 0.01
    ranges = np.array([(c["high"] - c["low"]) / c["close"] for c in candles[-2000:] if c["close"] > 0])
    volume = float(np.median([c.get("volume", 0.0) for c in candles[-2000:]])) if candles else 0.0
    return returns, max(spread, 1e-6), float(np.median(ranges)) if ranges.size else 0.01, volume


def _shocks(count, spread, rng):
    """Per-candle shocks like a real market's: fat-tailed (Student-t), in calm and violent stretches
    (GARCH-style volatility clustering), with a long-run spread of `spread`."""
    if count <= 0:
        return np.zeros(0)
    draws = rng.standard_t(4, count) / math.sqrt(2)  # t(4) scaled to unit variance
    alpha, beta = 0.10, 0.85
    variance, out = spread ** 2, np.empty(count)
    for index, draw in enumerate(draws):
        out[index] = draw * math.sqrt(variance)
        variance = spread ** 2 * (1 - alpha - beta) + alpha * out[index] ** 2 + beta * variance
    return out


# Days over which a scenario's wander is pulled halfway back to its path: long enough for real swings and
# relief rallies, short enough that a long phase keeps its shape.
WANDER_HALF_LIFE_DAYS = 10


def _wander(count, spread, volatility, per_day, rng):
    """A path of `count` candles that wanders around 0 like the market, then is pinned to end at 0 so it can
    ride on a phase without changing where it ends.

    Every candle keeps the market's own move size (`spread` x `volatility`, with its fat tails and
    calm and violent stretches); the wander is held near the phase's path by pulling it back
    (halfway over WANDER_HALF_LIFE_DAYS), never by shrinking the moves, which would look smooth.
    """
    if count <= 1 or volatility <= 0:
        return np.zeros(count + 1)
    pull = 0.5 ** (1 / max(WANDER_HALF_LIFE_DAYS * per_day, 1))
    level, path = 0.0, [0.0]
    for shock in _shocks(count, spread * volatility, rng):
        level = pull * level + shock
        path.append(level)
    path = np.array(path)
    return path - np.linspace(0, 1, count + 1) * path[-1]


def _phase(move, count, shape, spread, volatility, per_day, rng):
    """Log-price moves per candle for one phase: a shaped trend of `move` over `count` candles plus wander.

    shape: "fast" (most of the move early, like a crash), "slow" (accelerating, like a pump's
    top), "curve" (eases in, like a recovery) or "even".
    """
    count = max(int(count), 1)
    u = np.linspace(0, 1, count + 1)
    if shape == "fast":
        trend = (1 - np.exp(-4 * u)) / (1 - math.exp(-4))
    elif shape == "slow":
        trend = (np.exp(3 * u) - 1) / (math.exp(3) - 1)
    elif shape == "curve":
        trend = (1 - np.exp(-2.5 * u)) / (1 - math.exp(-2.5))
    else:
        trend = u
    return np.diff(move * trend + _wander(count, spread, volatility, per_day, rng))


def _moves(scenario, per_day, rng, history_returns, spread):
    """Log-price moves per candle for one scenario module."""
    kind, vol = scenario["type"], scenario.get("volatility", 1.0)
    candles = lambda days: max(1, int(round(days * per_day)))  # noqa: E731
    if kind == "crash":
        down = math.log(1 - scenario["drop"])
        # Regains `recovery` of the lost price: -50% then half recovered ends at -25%.
        up = math.log((1 - scenario["drop"] + scenario["recovery"] * scenario["drop"]) / (1 - scenario["drop"]))
        return np.concatenate([_phase(down, candles(scenario["days"]), "fast", spread, vol, per_day, rng),
                               _phase(up, candles(scenario["recovery_days"]), "curve", spread, vol, per_day, rng)])
    if kind == "bear":
        # A grind lower: several legs down with relief rallies between them.
        total, count = math.log(1 - scenario["drop"]), candles(scenario["days"])
        legs = max(1, min(6, int(scenario["days"] / 60)))
        weights = rng.dirichlet(np.ones(legs) * 3)
        sizes = np.maximum(1, np.round(weights * count).astype(int))
        sizes[-1] = max(1, count - sizes[:-1].sum())
        return np.concatenate([_phase(total * w, n, "fast", spread, vol, per_day, rng)
                               for w, n in zip(weights, sizes)])
    if kind == "pump":
        return np.concatenate([_phase(math.log(1 + scenario["rise"]), candles(scenario["days"]), "slow", spread, vol,
                                      per_day, rng),
                               _phase(math.log(1 - scenario["drop"]), candles(scenario["collapse_days"]), "fast",
                                      spread, vol, per_day, rng)])
    if kind == "sideways":
        # Wanders within about +-range with the market's own daily moves: pulled back toward the start so its
        # usual swing is about two-thirds of the range, softly held inside it (no hard edges), pinned to end at 0.
        count, limit = candles(scenario["days"]), math.log(1 + scenario["range"])
        step = spread * max(vol, 0.05)
        target = limit / 1.5
        pull = math.sqrt(1 - (step / target) ** 2) if step < target else 0.5
        level, path = 0.0, [0.0]
        for shock in _shocks(count, step, rng):
            # Bounded at every step, so a violent stretch bounces off the range's edges instead of sticking.
            level = limit * math.tanh((pull * level + shock) / limit)
            path.append(level)
        path = np.array(path)
        path -= np.linspace(0, 1, count + 1) * path[-1]
        return np.diff(path)
    if kind == "volatility":
        count = candles(scenario["days"])
        base = history_returns - history_returns.mean() if history_returns.size else None
        draws = rng.choice(base, size=count) if base is not None else _shocks(count, spread, rng)
        return draws * vol
    if kind == "resample":
        count, block = candles(scenario["days"]), candles(scenario["block_days"])
        moves = []
        while len(moves) < count and history_returns.size > block:
            start = rng.integers(0, history_returns.size - block)
            moves.extend(history_returns[start:start + block])
        return np.array(moves[:count]) if moves else _shocks(count, spread, rng)
    raise ScenarioError(f"unknown type {kind}")


def _candles(start_time, step_seconds, open_price, moves, rng, spread, high_low, volume):
    """OHLC candles following `moves` (log returns per candle) from `open_price`; bigger moves get longer wicks."""
    out, price = [], open_price
    for index, move in enumerate(moves):
        close = price * math.exp(move)
        size = high_low / 2 * min(max(1.0, abs(move) / spread), 3.0)
        # Wicks grow with a candle's move but stay within reach of the market's usual range: an unbounded
        # draw made rare 40-50% spikes inside a single candle that real markets almost never print.
        cap = 2 * high_low + abs(move) / 2
        wick_up, wick_down = (min(abs(rng.normal(0, size)), cap) for _ in range(2))
        out.append({"start": int(start_time + index * step_seconds), "open": price, "close": close,
                    "high": max(price, close) * (1 + wick_up), "low": max(min(price, close) * (1 - wick_down), 1e-12),
                    "volume": volume * (1 + min(abs(move) / spread, 5) / 2)})
        price = close
    return out


# --- runs: single scenarios and chains -------------------------------------------------

def runs(scenarios):
    """The runs a scenario list makes, in order: each a list of modules. A module with "chain" true continues
    the run of the module above it; any other module starts a new run."""
    out = []
    for scenario in scenarios:
        if scenario.get("chain") and out:
            out[-1].append(scenario)
        else:
            out.append([scenario])
    return out


def run_label(run):
    return " -> ".join(module["name"] for module in run)


def run_paths(run):
    """How many paths a run takes: the most of any of its modules (resample modules run several)."""
    return max(paths(module) for module in run)


def build(candles, run, granularity_seconds, path=0):
    """(candles, measure_from) for one path of a run (a scenario, or a chained list of them) over a market's
    real `candles`, oldest first.

    The run's modules follow one another, each starting where the one before ended. The whole run
    is appended after history, or written in from the first module's `at` date. `measure_from` is
    the run's first candle.
    """
    run = [normalize(module) for module in (run if isinstance(run, list) else [run])]
    first_module = run[0]
    per_day = 86400 / granularity_seconds
    history_returns, spread, high_low, volume = _profile(candles)
    pieces = []
    for number, module in enumerate(run):
        rng = np.random.default_rng([module["seed"], path, number])
        pieces.append(_moves(module, per_day, rng, history_returns, spread))
    moves = np.concatenate(pieces)
    rng = np.random.default_rng([first_module["seed"], path, 999])
    if first_module["placement"] == "append":
        last = candles[-1]
        start = last["start"] + granularity_seconds
        made = _candles(start, granularity_seconds, last["close"], moves, rng, spread, high_low, volume)
        return list(candles) + made, start
    at = int(datetime.strptime(first_module["at"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    first = next((i for i, c in enumerate(candles) if c["start"] >= at), None)
    if first is None or first == 0:
        raise ScenarioError(f"{first_module['name']}: {first_module['at']} is outside the history "
                            f"({datetime.fromtimestamp(candles[0]['start'], timezone.utc):%Y-%m-%d} to "
                            f"{datetime.fromtimestamp(candles[-1]['start'], timezone.utc):%Y-%m-%d})")
    steps = min(len(moves), len(candles) - first)
    made = _candles(candles[first]["start"], granularity_seconds, candles[first - 1]["close"], moves[:steps],
                    rng, spread, high_low, volume)
    # Later history continues from where the run ended, keeping its own shape.
    after = candles[first + steps:]
    if after:
        scale = made[-1]["close"] / candles[first + steps - 1]["close"]
        after = [{**c, **{k: c[k] * scale for k in ("open", "high", "low", "close")}} for c in after]
    return list(candles[:first]) + made + after, candles[first]["start"]


class MemoryMetrics:
    """Indicator values shared by the models run on one set of synthetic candles, kept in memory only:
    synthetic prices must never read or write the real market's stored indicator columns."""

    def __init__(self):
        self._caches = {}

    def cache(self, candles):
        from indicators import MetricCache
        key = (len(candles), candles[0]["start"], candles[-1]["start"])
        if key not in self._caches:
            self._caches[key] = MetricCache(candles)
        return self._caches[key]


def market_candles(candles, run, granularity_seconds, path=0, store=None, market=None):
    """build(), as SyntheticCandles with their own in-memory indicator cache (see MemoryMetrics).

    With a SyntheticStore `store` and `market` (product, granularity), the candles the run made
    or changed are also kept there, in their own table (see SyntheticStore).
    """
    built, start = build(candles, run, granularity_seconds, path)
    if store is not None:
        first = next(index for index, candle in enumerate(built) if candle["start"] >= start)
        store.save(*market, run, path, candles, built[first:])
    return SyntheticCandles(built, MemoryMetrics()), start


def _synthetic_candles_class():
    from candle_store import MarketCandles

    class SyntheticCandles(MarketCandles):
        """Candles with a scenario in them. They carry an in-memory indicator cache, and the real
        market's store refuses them (see candle_store.MarketMetrics.cache and CandleStore.save)."""
        synthetic = True

    return SyntheticCandles


SyntheticCandles = _synthetic_candles_class()

SYNTHETIC_DIR = Path(__file__).parent / "data" / "synthetic"
SYNTHETIC_DATABASE = "synthetic.db"
CANDLE_COLUMNS = ("start", "open", "high", "low", "close", "volume")
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS synthetic_runs (
    table_name TEXT PRIMARY KEY,
    product_id TEXT, granularity TEXT, run_label TEXT, path INTEGER,
    modules TEXT,             -- the run's scenario modules, as JSON
    history_from INTEGER,     -- the real history it was built on: first and last candle start,
    history_through INTEGER,  -- and how many candles
    history_candles INTEGER,
    first_start INTEGER, candles INTEGER,  -- the candles stored: those the run made or changed
    created TEXT
);
"""


class SyntheticStore:
    """The candles scenario runs make, kept apart from the real history: a separate database,
    data/synthetic/synthetic.db, never the price history's data/candles.db.

    Each market, run and path has its own table, named "<product>_<granularity>__<key>_p<path>",
    where the key is a hash of the run's modules and of the history it was built on; the table
    holds the candles the run made, or changed (for a run written in at a date, from there to
    the end). synthetic_runs describes every table. Building is seeded, so the same run on the
    same history makes the same candles, and a table already stored is left as it is.
    """

    def __init__(self, folder=None):
        self.folder = Path(folder if folder is not None else SYNTHETIC_DIR)
        self.path = self.folder / SYNTHETIC_DATABASE

    def _connect(self):
        from candle_store import BUSY_TIMEOUT_SECONDS, use_wal
        self.folder.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        use_wal(connection)
        connection.executescript(RUNS_SCHEMA)
        return connection

    @staticmethod
    def table_name(product_id, granularity, run, path, history):
        modules = [normalize(module) for module in (run if isinstance(run, list) else [run])]
        fingerprint = json.dumps([modules, path, len(history), history[0]["start"], history[-1]["start"],
                                  history[-1]["close"]], sort_keys=True, default=str)
        key = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
        return f"{product_id}_{granularity}__{key}_p{path}"

    def save(self, product_id, granularity, run, path, history, candles):
        """Keep `candles` (the run's own) in the run's table, unless it is stored already; returns the table."""
        table = self.table_name(product_id, granularity, run, path, history)
        quoted = '"' + table.replace('"', '""') + '"'
        modules = [normalize(module) for module in (run if isinstance(run, list) else [run])]
        connection = self._connect()
        try:
            with connection:
                if connection.execute("SELECT 1 FROM synthetic_runs WHERE table_name = ?", (table,)).fetchone():
                    return table
                connection.execute(f"CREATE TABLE IF NOT EXISTS {quoted} (start INTEGER PRIMARY KEY, open REAL, "
                                   "high REAL, low REAL, close REAL, volume REAL)")
                connection.executemany(f"INSERT OR IGNORE INTO {quoted} VALUES (?, ?, ?, ?, ?, ?)",
                                       ([candle[column] for column in CANDLE_COLUMNS] for candle in candles))
                connection.execute(
                    "INSERT OR IGNORE INTO synthetic_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (table, product_id, granularity, run_label(modules), path, json.dumps(modules, default=str),
                     history[0]["start"], history[-1]["start"], len(history),
                     candles[0]["start"] if candles else None, len(candles),
                     time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
        finally:
            connection.close()
        return table

    def runs(self):
        """Every stored run: [{"table_name", "product_id", ..., "created"}], in the order they were stored."""
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            cursor = connection.execute("SELECT * FROM synthetic_runs ORDER BY rowid")
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            connection.close()

    def size(self):
        """The bytes the synthetic store takes on disk."""
        return sum(path.stat().st_size for path in self.folder.glob(SYNTHETIC_DATABASE + "*")) \
            if self.folder.exists() else 0

    def clear(self):
        """Delete every stored synthetic run (the whole database). Returns how many there were."""
        count = len(self.runs())
        for path in self.folder.glob(SYNTHETIC_DATABASE + "*") if self.folder.exists() else ():
            path.unlink()
        return count

    def load(self, table):
        """A stored run's candles, oldest first."""
        connection = self._connect()
        try:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = connection.execute(f"SELECT {', '.join(CANDLE_COLUMNS)} FROM {quoted} ORDER BY start").fetchall()
        finally:
            connection.close()
        return [dict(zip(CANDLE_COLUMNS, row)) for row in rows]


def paths(scenario):
    """How many paths a scenario runs (resample scenarios run several)."""
    return int(normalize(scenario).get("paths", 1)) if scenario.get("type") == "resample" else 1

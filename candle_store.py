"""The local candle database: one SQLite table per product and candle size.

    data/candles.db
        "BTC-USD_FIFTEEN_MINUTE"   a row per candle: start, open, high, low, close, volume,
                                   then a column for each stored metric
        candle_tables              the time range downloaded for each candle table
        metric_columns             the candles each stored metric has been computed for

The models in a run share their indicators through one cache per market (MarketMetrics),
so each distinct indicator is computed once per run. An indicator that is slow to compute
and depends only on a fixed number of recent candles is also stored, as a column of the
candle table named by its metric key, such as "CCI(period=2880)". Later runs read it back
and compute only the candles it is missing; candles added since a model last needed it stay
blank until one does. Indicators that depend on the whole history before them (EMA-based
and Wilder-smoothed ones, OBV and VWAP) are never stored, because filling in new candles
would mean recomputing them from the start. Within a column's computed range an empty cell
means the metric has no value there (its warm-up); outside it, the cell was not computed.

Stored values match a fresh computation to within floating-point rounding. Before missing
candles are filled in, the last stored values are recomputed and compared, and if they
differ the whole column is recomputed instead.

Concurrent runs (see parallel_backtest.py) give the database a single writer. The database
is kept in WAL mode, so readers never block it or each other. A store opened with
`read_only` never writes: it reads candles and stored metrics, and holds back the metric
columns it would have stored (take_pending_metrics) for the writer to apply.
"""

import bisect
import contextlib
import csv
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from indicators import MetricCache

DATABASE_NAME = "candles.db"
CANDLE_FIELDS = ["start", "open", "high", "low", "close", "volume"]
# A metric is stored when computing it takes longer than this per candle: about twice what
# reading a stored column back costs (0.4 microseconds per candle), so storing pays off.
STORE_SECONDS_PER_CANDLE = 1e-6
# Stored values just before the missing ones that are recomputed and compared before a
# stored metric is extended.
CHECKED_CANDLES = 50
# How long a connection waits for another one's write to finish before giving up.
BUSY_TIMEOUT_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS candle_tables (
    table_name TEXT PRIMARY KEY,
    fetched_start INTEGER NOT NULL,
    fetched_end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_columns (
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    computed_from INTEGER NOT NULL,
    computed_through INTEGER NOT NULL,
    PRIMARY KEY (table_name, column_name)
);
"""


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _read_csv_cache(cache_dir, table):
    """(candles, fetched_start, fetched_end) from the CSV cache of earlier versions, or None."""
    csv_path, meta_path = Path(cache_dir) / f"{table}.csv", Path(cache_dir) / f"{table}.meta.json"
    if not csv_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with csv_path.open(newline="", encoding="utf-8") as handle:
        candles = [
            {"start": int(row["start"]), **{field: float(row[field]) for field in CANDLE_FIELDS[1:]}}
            for row in csv.DictReader(handle)
        ]
    return candles, int(meta["fetched_start"]), int(meta["fetched_end"])


class CandleStore:
    """The candle database in `cache_dir`; with `read_only`, a reader that never writes to it."""

    def __init__(self, cache_dir, read_only=False):
        self.cache_dir = Path(cache_dir)
        self.path = self.cache_dir / DATABASE_NAME
        self.read_only = read_only
        self._pending_metrics = []  # write_metrics calls a read-only store held back

    @staticmethod
    def table_name(product_id, granularity):
        return f"{product_id}_{granularity}"

    @contextlib.contextmanager
    def _connect(self):
        """A connection that commits if the block succeeds and is always closed.

        A read-only store's connection refuses writes (query_only) and leaves the schema alone.
        """
        if not self.read_only:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            if self.read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                # WAL persists in the file: readers see the last commit while a write is under way.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self, product_id, granularity):
        """Return (candles, fetched_start, fetched_end) for the market, or None if it has none.

        A CSV cache written by an earlier version is imported the first time; its files are
        left in place but no longer read. A read-only store doesn't import it.
        """
        table = self.table_name(product_id, granularity)
        if self.read_only and not self.path.exists():
            return None  # connecting would create an empty database file
        with self._connect() as connection:
            if self.read_only and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'candle_tables'").fetchone():
                return None
            fetched = connection.execute(
                "SELECT fetched_start, fetched_end FROM candle_tables WHERE table_name = ?", (table,)).fetchone()
            if fetched is None and self.read_only:
                return None
            if fetched is None:
                imported = _read_csv_cache(self.cache_dir, table)
                if imported is None:
                    return None
                self._insert(connection, table, *imported)
                fetched = imported[1:]
            rows = connection.execute(
                f"SELECT start, open, high, low, close, volume FROM {_quote(table)} ORDER BY start").fetchall()
        candles = [{"start": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                   for r in rows]
        return candles, fetched[0], fetched[1]

    def save(self, product_id, granularity, candles, fetched_start, fetched_end):
        """Add candles to the market's table and record the time range now downloaded.

        The range is kept because periods with no trades have no candles; without it a
        product younger than the lookback would refetch every run. A candle already in the
        table is kept as it is, with its stored metric values: a closed candle doesn't change.
        """
        if self.read_only:
            raise PermissionError(f"{self.path} is open read-only; candles are saved by the run's writer")
        with self._connect() as connection:
            self._insert(connection, self.table_name(product_id, granularity), candles, fetched_start, fetched_end)

    @staticmethod
    def _insert(connection, table, candles, fetched_start, fetched_end):
        connection.execute(f"CREATE TABLE IF NOT EXISTS {_quote(table)} "
                           "(start INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        connection.executemany(
            f"INSERT OR IGNORE INTO {_quote(table)} (start, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?)",
            ([candle[field] for field in CANDLE_FIELDS] for candle in candles))
        connection.execute("INSERT OR REPLACE INTO candle_tables VALUES (?, ?, ?)", (table, fetched_start, fetched_end))

    def metric_ranges(self, table, columns):
        """{column: (first, last)} start times of the candles computed, for those of `columns` stored."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT column_name, computed_from, computed_through FROM metric_columns "
                f"WHERE table_name = ? AND column_name IN ({', '.join('?' * len(columns))})",
                (table, *columns)).fetchall()
        return {column: (first, last) for column, first, last in rows}

    def read_metric(self, table, column, first, last):
        """The column's values for the candles starting from `first` through `last`, NaN where empty."""
        with self._connect() as connection:
            # int(): sqlite3 would bind a numpy integer as a blob, which matches no start time.
            cursor = connection.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} WHERE start BETWEEN ? AND ? ORDER BY start",
                (int(first), int(last)))
            return np.fromiter((np.nan if value is None else value for (value,) in cursor), dtype=float)

    def write_metrics(self, table, columns, extend=False):
        """Store metric values; `columns` maps each column name to (start times, values).

        A new column is added to the table. An existing one has its values replaced, or with
        `extend`, these values continue its computed range. An extension that no longer joins
        the stored range (another run replaced the column meanwhile) is skipped.

        A read-only store holds the call back instead; see take_pending_metrics.
        """
        if self.read_only:
            self._pending_metrics.append((table, columns, extend))
            return
        with self._connect() as connection:
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")}
            for column, (starts, values) in columns.items():
                first, last = int(starts[0]), int(starts[-1])
                if column not in existing:
                    connection.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(column)} REAL")
                elif extend:
                    stored = connection.execute(
                        "SELECT computed_from, computed_through FROM metric_columns "
                        "WHERE table_name = ? AND column_name = ?", (table, column)).fetchone()
                    before = connection.execute(
                        f"SELECT MAX(start) FROM {_quote(table)} WHERE start < ?", (first,)).fetchone()[0]
                    if stored is None or (before is not None and stored[1] < before):
                        continue
                    first = stored[0]
                else:
                    connection.execute(
                        f"UPDATE {_quote(table)} SET {_quote(column)} = NULL WHERE {_quote(column)} IS NOT NULL")
                # SQLite stores NaN as NULL, so warm-up candles come back empty.
                connection.executemany(f"UPDATE {_quote(table)} SET {_quote(column)} = ? WHERE start = ?",
                                       zip(values.tolist(), starts.tolist()))
                connection.execute("INSERT OR REPLACE INTO metric_columns VALUES (?, ?, ?, ?)",
                                   (table, column, first, last))

    def take_pending_metrics(self):
        """The write_metrics calls a read-only store held back, as (table, columns, extend), clearing them.

        A writable store for the same database applies them with apply_metrics.
        """
        pending, self._pending_metrics = self._pending_metrics, []
        return pending

    def apply_metrics(self, pending):
        """Store metric columns held back by read-only stores (see take_pending_metrics)."""
        for table, columns, extend in pending:
            self.write_metrics(table, columns, extend=extend)


def _by_output(result):
    return result if isinstance(result, dict) else {None: result}


def _from_outputs(outputs):
    return outputs[None] if None in outputs else outputs


def _same(stored, recomputed):
    """Whether stored values match recomputed ones to within floating-point rounding."""
    both = np.concatenate([stored, recomputed])
    finite = np.abs(both[np.isfinite(both)])
    scale = max(finite.max(), 1.0) if finite.size else 1.0
    return np.allclose(stored, recomputed, rtol=1e-9, atol=1e-9 * scale, equal_nan=True)


class StoredMetricCache(MetricCache):
    """A MetricCache for consecutive candles of a stored table, keeping slow metrics as columns.

    The values it gives are the ones the metric computed over just these candles gives, as
    with a plain MetricCache: stored values that fall within a metric's warm-up on these
    candles are not used.
    """

    def __init__(self, candles, store, table):
        super().__init__(candles)
        self.store, self.table = store, table

    @staticmethod
    def _column(key, output):
        return f"{key}.{output}" if output else key

    def _compute(self, key, compute, lookback):
        if lookback is None or len(self) < max(lookback.values()):
            return compute()  # it depends on the whole history, or never warms up on these candles
        columns = {output: self._column(key, output) for output in lookback}
        ranges = self.store.metric_ranges(self.table, list(columns.values()))
        if len(ranges) == len(columns):
            result = self._read(columns, ranges, compute, lookback)
            if result is not None:
                return result
        started = time.perf_counter()
        result = compute()
        if ranges or time.perf_counter() - started > len(self) * STORE_SECONDS_PER_CANDLE:
            outputs = _by_output(result)
            times = self.arrays["time"]
            self.store.write_metrics(self.table, {
                column: (times[lookback[output] - 1:], outputs[output][lookback[output] - 1:])
                for output, column in columns.items()})
        return result

    def _read(self, columns, ranges, compute, lookback):
        """The stored metric for these candles, computing and storing the candles it is missing.

        Returns None when the stored values can't be used, and the metric is recomputed.
        """
        times = self.arrays["time"]
        count = len(self)
        # The first of these candles past the stored range; the same for every output.
        missing = bisect.bisect_right(times, min(last for _, last in ranges.values()))
        # Missing candles are computed from enough earlier ones to warm up, and to recompute
        # the last CHECKED_CANDLES stored values for comparison.
        begin = missing - CHECKED_CANDLES - (max(lookback.values()) - 1)
        if missing < count and begin < 0:
            return None
        tail = _by_output(compute(begin)) if missing < count else None
        outputs, additions = {}, {}
        for output, column in columns.items():
            warm = lookback[output] - 1
            low = warm if tail is None else min(warm, missing - CHECKED_CANDLES)
            if ranges[column][0] > times[low]:
                return None  # the column doesn't reach back to these candles' warm-up
            stored = self.store.read_metric(self.table, column, times[low], times[missing - 1])
            if len(stored) != missing - low:
                return None  # the table doesn't hold these candles
            series = np.full(count, np.nan)
            series[warm:missing] = stored[warm - low:]
            if tail is not None:
                recomputed = tail[output][missing - begin - CHECKED_CANDLES:missing - begin]
                if not _same(stored[-CHECKED_CANDLES:], recomputed):
                    return None
                series[missing:] = tail[output][missing - begin:]
                additions[column] = (times[missing:], series[missing:])
            outputs[output] = series
        if additions:
            self.store.write_metrics(self.table, additions, extend=True)
        return _from_outputs(outputs)


class MarketMetrics:
    """The metric caches for one market's candles, shared by every model run on them."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self._caches = {}

    def cache(self, candles):
        """The shared cache for `candles`, one per distinct candle range."""
        key = (len(candles), candles[0]["start"], candles[-1]["start"])
        if key not in self._caches:
            self._caches[key] = StoredMetricCache(candles, self.store, self.table)
        return self._caches[key]


class MarketCandles(list):
    """Candles loaded from the store, with the `metrics` every model backtested on them shares."""

    def __init__(self, candles, metrics):
        super().__init__(candles)
        self.metrics = metrics

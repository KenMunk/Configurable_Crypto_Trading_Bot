"""The evaluations every model search on this computer shares: data/model_search/registry.db.

Searches running at the same time, and later searches, look a candidate up here before
backtesting it, and each records what it scored. So no model is backtested twice under the
same conditions, and each search can borrow the best models the others have found ("migrants")
as parents.

An evaluation is only reused under identical conditions: the registry is keyed by a context
that hashes the market, the test windows, the fitness settings, the template's costs, the
fees and the last candle of each candle size searched. Change any of them and a search starts
a fresh context. The database is in WAL mode and every write is one short transaction, so
any number of searches can share it.
"""

import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from candle_store import use_wal

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    context TEXT NOT NULL,
    key TEXT NOT NULL,
    instance TEXT NOT NULL,
    genome TEXT NOT NULL,
    train TEXT NOT NULL,
    holdout TEXT NOT NULL,
    fitness REAL NOT NULL,
    recorded REAL NOT NULL,
    PRIMARY KEY (context, key)
);
CREATE INDEX IF NOT EXISTS evaluations_by_fitness ON evaluations (context, fitness);
"""
CHUNK = 500  # keys per lookup query, under SQLite's parameter limit


def context_key(**parts):
    """A short hash of everything that decides a backtest's result."""
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class SearchRegistry:
    """The registry's evaluations in one context, as seen by one search (`instance`)."""

    def __init__(self, path, context, instance):
        self.path, self.context, self.instance = Path(path), context, instance

    @contextlib.contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            use_wal(connection)
            connection.executescript(SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    def lookup(self, keys):
        """{key: {"train": summary, "holdout": summary}} for the keys already evaluated."""
        keys, found = list(keys), {}
        with self._connect() as connection:
            for start in range(0, len(keys), CHUNK):
                chunk = keys[start:start + CHUNK]
                rows = connection.execute(
                    f"SELECT key, train, holdout FROM evaluations WHERE context = ? "
                    f"AND key IN ({', '.join('?' * len(chunk))})", (self.context, *chunk))
                for key, train, holdout in rows:
                    found[key] = {"train": json.loads(train), "holdout": json.loads(holdout)}
        return found

    def record(self, evaluations):
        """Store {"key", "genome", "train", "holdout"} evaluations, ordered by their "fitness" (default:
        the training fitness); one already stored is kept."""
        now = time.time()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ((self.context, e["key"], self.instance, json.dumps(e["genome"]), json.dumps(e["train"]),
                  json.dumps(e["holdout"]), e.get("fitness", e["train"]["fitness"]), now) for e in evaluations))

    def best(self, limit, others_only=True, key=None):
        """The best evaluations, by default those other searches recorded.

        They are ordered by the fitness stored with them, or by `key(record)` when given (for
        fitness settings other than the recording search's): then the best few hundred by
        stored fitness are re-ranked.
        """
        query = "SELECT key, instance, genome, train, holdout FROM evaluations WHERE context = ?"
        params = [self.context]
        if others_only:
            query += " AND instance != ?"
            params.append(self.instance)
        fetch = limit if key is None else max(limit * 20, 200)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY fitness DESC LIMIT ?", (*params, fetch)).fetchall()
        records = [{"key": k, "instance": instance, "genome": json.loads(genome), "train": json.loads(train),
                    "holdout": json.loads(holdout)} for k, instance, genome, train, holdout in rows]
        if key is not None:
            records = sorted(records, key=key, reverse=True)[:limit]
        return records

    def count(self):
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM evaluations WHERE context = ?",
                                      (self.context,)).fetchone()[0]

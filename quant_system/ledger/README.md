# quant_system/ledger/: the trader's ledger

This folder holds `fortress.db`, the SQLite ledger that `python engine.py` creates in its base directory, [quant_system/](../README.md). The engine creates it as soon as it is constructed, before it loads profiles. The simulator does not use this file; it uses the repository's [ledger/](../../ledger/README.md) folder.

## What it contains

Nothing yet. At start-up the engine turns on write-ahead logging, which keeps disk wear low, but no tables exist. The engine does not record trades yet. See [The ledger database](../../Project_Documentation/trader.md#7-the-ledger-database).

The [quant infrastructure blueprint](../../Project_Documentation/quant_infrastructure_blueprint.md) plans for this file to become the node's master record:

- Writes are batched into one daily flush at midnight, to protect solid-state storage.
- During a hardware replacement, the file is copied to the new node with `scp` and kept in sync nightly until handover.

While a connection is open, SQLite may create `fortress.db-wal` and `fortress.db-shm` beside the database. They are part of write-ahead logging.

## In Git

The empty database is tracked. If it is deleted, it is recreated automatically the next time the engine starts. Git does not ignore the `fortress.db-wal` and `fortress.db-shm` files, so don't commit them if they appear. Once it holds real trading records, keep it out of the repository.

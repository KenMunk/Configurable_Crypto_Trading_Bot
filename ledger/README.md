# ledger/: ledger database for the repository-root base directory

This folder holds `fortress.db`, an SQLite database created by the trader engine's storage controller (`QuantitativeStorageController` in `engine.py`).

## Why it is here

The engine creates `<base directory>/ledger/fortress.db` when it starts. Two things use the repository root as their base directory:

- **The simulator.** It creates an engine with `base_directory="."` so it can reuse the engine's Coinbase authentication code. That creates this database as a side effect.
- **The engine launcher** in the [trader guide](../Project_Documentation/trader.md#keeping-the-engine-running), which also uses `base_directory="."`.

Running `python engine.py` uses [quant_system/ledger/](../quant_system/ledger/README.md) instead.

The path is relative to the **current working directory**. If you start the simulator from another folder, a new `ledger/` folder appears there instead. Run the tools from the repository root.

## What it contains

Nothing yet. The database is set up for low disk wear, with write-ahead logging, but has no tables. The [quant infrastructure blueprint](../Project_Documentation/quant_infrastructure_blueprint.md) plans for it to hold trading records, flushed to disk once a day. See [The ledger database](../Project_Documentation/trader.md#7-the-ledger-database) for details.

While a connection is open, SQLite may create `fortress.db-wal` and `fortress.db-shm` beside the database. They are part of write-ahead logging.

## In Git

The empty database is tracked. If it is deleted, it is recreated automatically the next time a tool starts. Once it holds real trading records, keep it out of the repository.

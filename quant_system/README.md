# quant_system/: the trader's default base directory

When you run the trader as `python engine.py`, it uses this folder as its **base directory**:

```python
QuantitativeTradingEngine(base_directory="./quant_system", serial_port="/dev/ttyS0", node_is_primary=True)
```

In the [quant infrastructure blueprint](../Project_Documentation/quant_infrastructure_blueprint.md), this folder is the headless execution node's working area. It holds the profiles the node runs and the node's ledger.

## Layout

| Path | In Git | Purpose |
|---|---|---|
| `config/` | **Ignored** | The model profiles the engine loads at start-up. It is created on the first run if it is missing. Copy validated profiles here from the repository's `config/` folder. |
| [ledger/](ledger/README.md) | Tracked (empty) | The engine's SQLite ledger, `fortress.db`. |

## Things to know

- **The path is relative to the current working directory.** Run `python engine.py` from the repository root to use this folder.
- **The engine reads profiles only at start-up.** It loads every `*.json` file in `config/`, keyed by each profile's `ticker`, and does not validate them. Validate each profile with the simulator's **Load Profile** button first.
- **A relative `COINBASE_KEY_FILE`** in `.env` is resolved against this folder when you run `python engine.py`. An absolute path avoids surprises.
- **To use the repository's own `config/` folder instead**, run the engine with the launcher script in the [trader guide](../Project_Documentation/trader.md#keeping-the-engine-running). It uses `base_directory="."`.

For the full picture, see the [trader guide](../Project_Documentation/trader.md).

# live_config/: the live trader's settings template

This folder holds the **template** for the live trader's settings. Your own settings don't live here: they live in the live trader's [data folder](../Project_Documentation/live_trading.md#3-your-data-folder), outside the program, together with its ledger, backups, logs and reports. Removing or updating the program never touches them.

| Path | In Git | Contents |
|---|---|---|
| `.example/live_trading.example.json` | Tracked | The settings template (your deployments). |
| `.example/live_trader.env.example` | Tracked | The template for the live trader's own configuration file: its API key and portal settings. The live trader never reads the simulator's `.env`. |
| `README.md` | Tracked | This file. |

To start, put both templates in your data folder and edit them there:

```bash
python live_trader.py --init
```

It prints where the files are: `live_config/live_trading.json` and `live_trader.env`. Each entry under `deployments` runs one model with its own capital. List as many as you like. Several can trade the same currency, and each keeps its own cash and coins in the ledger. The [live trading guide](../Project_Documentation/live_trading.md) explains every field, how the API key decides between live and paper trading, and the safeguards.

An earlier version kept `live_trading.json`, the `HALT` file and the ledger here; the first start of this version moves them to the data folder.

# config/: model profiles

This folder holds the **model profiles**: JSON files that each describe one complete trading strategy. The simulator lists every `*.json` file in this folder (not in subfolders) in its **Models** list.

## What's here

| Item | In Git | Purpose |
|---|---|---|
| `*.json` | **Ignored** | Your private model profiles. Git never sees them. |
| [.example/](.example/) | Tracked | Public template profiles to copy from. |
| `.gitkeep` | Tracked | Keeps this folder in the repository while it holds no tracked profiles. |
| `README.md` | Tracked | This file. |

`.gitignore` excludes everything in this folder except the three tracked items above. Strategies you save here stay on your machine, so this is the place for proprietary models. Never put them in `.example/`, which is public.

## Adding a model

1. Copy a template into this folder under a new name. The file name, without `.json`, becomes the model's name in the simulator:

   ```bash
   cp config/.example/btc_macro_horizon.example.json config/my_model.json
   ```

2. Edit the profile. Every field is documented in the [model configuration guide](../Project_Documentation/model_configuration.md), and every indicator and rule function in the [indicator reference](../Project_Documentation/indicator_reference.md).
3. In the simulator, click **Refresh**, select the model, and click **Load Profile** to validate it.

When the simulator opens, it pre-selects `btc_macro_horizon.json` if that file exists. To test variants side by side, save them as separate files and select them together.

## Which tools read this folder

| Tool | Reads |
|---|---|
| Simulator (`python simulator.py`) | This folder. |
| Trader launcher from the [trader guide](../Project_Documentation/trader.md#keeping-the-engine-running) (`base_directory="."`) | This folder. |
| Trader run as `python engine.py` | `quant_system/config/` instead (see [quant_system/](../quant_system/README.md)). |

## Keep secrets out

Profiles describe strategies only. API keys and other secrets belong in the `.env` file at the repository root; see [Setting up the .env file](../Project_Documentation/trader.md#2-setting-up-the-env-file).

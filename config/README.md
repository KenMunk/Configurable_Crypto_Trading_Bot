# config/: model profiles

This folder holds the **model profiles**: JSON files that each describe one complete trading strategy. They are kept in **one folder per currency**, named after the product the models trade:

```text
config/
├── DOGE-USD/             the DOGE-USD models
│   ├── discard/          models a ranking set aside, with discard_log.csv
│   ├── regimes/          models with market regimes (experimental)
│   └── best_model.json   the best DOGE-USD model the simulator has tested
├── BTC-USD/ ...
├── *.json                profiles from before currency folders (still read)
└── .example/             public templates
```

The simulator lists the models in every currency folder, plus any profiles still at the top of this folder, in its **Models** list, with a **Currency** filter and a **Type** filter for models with or without regimes. The model search writes new models into its currency's folder. To move older profiles at the top of this folder (and in `config/discard/`) into their currency's folders, run:

```bash
python model_assessor.py --organize
```

A profile without a `product_id`, or whose name is already taken in its currency's folder, stays where it is and is listed.

## What's here

| Item | In Git | Purpose |
|---|---|---|
| `*.json` | **Ignored** | Your private model profiles. Git never sees them. |
| [.example/](.example/) | Tracked | Public template profiles to copy from. |
| `<PRODUCT>/` | **Ignored** | One folder per currency, such as `DOGE-USD/`, holding its models. |
| `<PRODUCT>/regimes/` | **Ignored** | Models with [market regimes](../Project_Documentation/model_configuration.md#49-regimes-experimental) (experimental). The simulator lists them with the rest. Keep them here, not at the top of the currency folder: a branch without regime support doesn't list this folder, so it can't silently trade such a model on its base rules alone. |
| `<PRODUCT>/discard/` | **Ignored** | Models the [ranking](../search_config/README.md#ranking-and-the-discard-folder) set aside, with `discard_log.csv` recording why. The simulator doesn't list them. Bring one back with `python model_assessor.py --restore <name>`. |
| `<PRODUCT>/best_model.json` | **Ignored** | The currency's best model, recorded by the simulator after a full-history run, and starred and pre-selected in its Models list. |
| `.models.lock` | **Ignored** | Exists only while a search or the assessor is naming, writing or moving models. |
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

When the simulator opens, it pre-selects `btc_macro_horizon.json` if that file exists, and otherwise the first model in the list. **Refresh** rescans this folder and keeps your current selection. To test variants side by side, save them as separate files and select them together.

To test a profile stored elsewhere, use **Add file...** in the simulator. It adds the file to the list for this session without copying it here.

## Which tools read this folder

| Tool | Reads |
|---|---|
| Simulator (`python simulator.py`) | This folder. |
| Trader launcher from the [trader guide](../Project_Documentation/trader.md#keeping-the-engine-running) (`base_directory="."`) | This folder. |
| Trader run as `python engine.py` | `quant_system/config/` instead (see [quant_system/](../quant_system/README.md)). |

## Keep secrets out

Profiles describe strategies only. API keys and other secrets belong in the `.env` file at the repository root; see [Setting up the .env file](../Project_Documentation/trader.md#2-setting-up-the-env-file).

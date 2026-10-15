# search_config/: model search settings

Settings files for `python model_search.py`, the evolutionary model search. A settings file decides everything about a search: which market, which time windows, which conditions models may be built from, the choices for every setting, how models are scored, and how they are bred and analysed. The code only provides the mechanism.

This folder is **private**. Like `config/`, it is gitignored apart from this README and the [.example/](.example/) template, so the search space you tune stays on your machine.

## Getting started

```bash
cp search_config/.example/model_search.example.json search_config/model_search.json
python model_search.py                                   # reads search_config/model_search.json
python model_search.py --settings search_config/eth.json # or any other settings file
python model_search.py --offline --dry-run               # cached data; writes only to the run folder
```

The template is a deliberately small search over daily BTC candles. It shows the format and is not a tuned search space.

Command-line options override single settings for one run: `--product`, `--prefix`, `--population`, `--keep`, `--generators`, `--evaluators`, `--batch-size`, `--min-generations`, `--max-generations` and `--seed`. Each run copies its effective settings to `data/model_search/<run>/settings.json`.

## The settings file

Every key below is required; the search stops with a list of anything missing.

| Section | Key | Meaning |
|---|---|---|
| `market` | `product` | Coinbase product to search, such as `DOGE-USD`. |
| | `prefix` | Output names: `config/<prefix>_001.json` and up. The search refuses to overwrite existing ones. |
| | `template_profile` | Profile (path from the repository root) whose friction, capital, tax and execution blocks every model copies. |
| | `granularities` | Candle sizes models may use, such as `["ONE_DAY", "SIX_HOUR"]`. |
| | `reference_fallback_granularity` | Candle size given to reference models whose own size isn't in `granularities`. |
| `windows` | `train_start`, `holdout_start` | Dates (`YYYY-MM-DD`). Models are scored and selected on `[train_start, holdout_start)`; the holdout, from `holdout_start` to the latest candle, is only reported. |
| `search` | `population` | Models bred per generation. |
| | `keep` | Best models written to `config/` at the end. |
| | `elites` | Best models so far that parents are drawn from. |
| | `generators`, `evaluators` | Worker processes that breed and that backtest (one analyst always runs). |
| | `batch_size` | Models per evaluator batch. |
| | `min_generations`, `max_generations` | Bounds on the number of generations. |
| | `patience`, `tolerance` | A plateau: neither the best score nor the top-20 mean has gained `tolerance` in `patience` generations. |
| | `seed` | Random seed for the coordinator. |
| `fitness` | `drawdown_weight` | Score = (log growth + `drawdown_weight` × log(1 − max drawdown)) per year … |
| | `min_trades`, `trade_penalty` | … less `trade_penalty` for each trade short of `min_trades`. |
| `breeding` | `mutate`, `crossover` | Shares of each generation bred by mutation and crossover; the rest are fresh. |
| | `tournament_size` | Elites compared when picking each parent. |
| | `max_atoms` | Most conditions in one vote. |
| | `confirm_sizes` | Sizes of the optional confirming vote. |
| | `mutations_per_child` | Picked from at random: how many changes each mutation makes. |
| | `neighbor_span` | How far a "neighbour" mutation moves along a family of conditions. |
| `analysis` | `top_fraction`, `shrink` | The top group the analyst compares against, and how strongly rare characteristics are pulled towards "no effect". |
| | `weight_floor`, `weight_cap` | Limits on the weights the analyst sends to the generators. |
| | `reference_half_weight_models` | After this many scored models, the existing models' evidence counts half as much as the new market's. |
| | `report_min_support`, `report_min_atom_support`, `reference_min_support` | Fewest models a characteristic needs to appear in a report table. |
| `options` | `entry_n`, `exit_n`, `entry_ratio`, `exit_ratio`, `exit_mode`, `confirm`, `entry_persist_h`, `exit_persist_h`, `lock`, `trail`, `stop`, `target`, `dd_limit`, `pos` | The choices for each part of a model: vote sizes and thresholds, how the exit is chosen (`own`, `same` as the entry, or the `confirm` vote), persistence windows in hours, the SMA regime lock in days, the trailing stop, hard stop, profit target, drawdown limit and position fraction. |
| `atoms` | | The conditions models are built from (below). |

## Writing atoms

An atom is one bullish condition that votes in an entry or exit rule. Each entry in `atoms` describes a family of them:

```json
{"family": "price_above_{type}", "expr": "close > {ma}",
 "indicators": {"ma": {"type": "{type}", "period": "{period}"}},
 "grid": {"type": ["SMA", "EMA"], "period": ["50d", "200d"]}}
```

- Every combination of the `grid` values gives one atom; this one gives four (`close > sma_50d` and so on).
- `cases` is a list of parameter sets used instead of, or as well as, a grid, for values that go together: `"cases": [{"fast_period": "50d", "slow_period": "200d"}]`.
- `{name}` placeholders are filled from the grid and cases. A value that is only a placeholder keeps its type, so `"period": "{period}"` with a grid value of `14` stays the number 14.
- Placeholders that name an entry in `indicators` become that indicator's canonical name (such as `sma_50d`). An indicator's `source` may name another one: `"source": "{obv}"`.
- `expr` uses the [rule language](../Project_Documentation/indicator_reference.md#5-writing-signal-rules), and indicators use the [indicator catalog](../Project_Documentation/indicator_reference.md#4-indicator-catalog).
- Atoms invalid on a candle size, such as a `12h` average on daily candles, are simply not used on it.

Conditions found in the existing models in `config/` are added to the library automatically.

# search_config/: model search settings and goals

Settings files for `python model_search.py`, the evolutionary model search, and goals files for `python model_assessor.py`, which checks models against the targets you set and ranks them (see [Goals](#goals-and-the-assessor)). The search uses the same goals to decide which models stay in `config/`. A settings file decides everything about a search: which market, which time windows, which conditions models may be built from, the choices for every setting, how models are scored, and how they are bred and analysed. The code only provides the mechanism.

This folder is **private**. Like `config/`, it is gitignored apart from this README and the [.example/](.example/) template, so the search space you tune stays on your machine.

## Getting started

```bash
cp search_config/.example/model_search.example.json search_config/model_search.json
python model_search.py                                   # reads search_config/model_search.json
python model_search.py --settings search_config/eth.json # or any other settings file
python model_search.py --offline --dry-run               # cached data; writes only to the run folder
python model_search.py --instances 3                     # three cooperating searches at once (see below)
python model_lab.py                                      # or queue and run searches in a window
```

[Model Lab](../Project_Documentation/model_lab.md) runs these same searches from a window: queue several settings files, run them at once, and browse the top 20 models against your goals.

The template is a deliberately small search over daily BTC candles. It shows the format and is not a tuned search space.

Command-line options override single settings for one run: `--product`, `--prefix`, `--population`, `--keep`, `--generators`, `--evaluators`, `--batch-size`, `--min-generations`, `--max-generations`, `--seed` and `--goals`. Each run copies its effective settings to `data/model_search/<run>/settings.json`. Three more options shape the run itself:

| Option | Effect |
|---|---|
| `--instances N` | Runs N searches at once, sharing the CPUs. See [Running searches in parallel](#running-searches-in-parallel). |
| `--references PATTERN ...` | Learns from these profiles only, such as `"config/doge_a_*.json"`, instead of every profile in `config/`. See [Choosing reference profiles and the models folder](#choosing-reference-profiles-and-the-models-folder). |
| `--models-dir DIR` | Writes, ranks and discards models in this folder instead of `config/`. |
| `--no-rank` | Skips the [ranking and discard](#ranking-and-the-discard-folder) at the end. |
| `--dry-run` | Writes the profiles to the run folder only, leaving `config/`, the performance log and the ranking alone. |

## What a search does at the end

1. **Keeps a variety of models.** From every model it scored, best first, it drops any that trade exactly like a better one: the same trades, return and drawdown in both windows, usually because of a redundant condition. It then skips any model whose conditions overlap an already-chosen one's by more than `variety.max_similarity`, and keeps the first `keep` that remain.
2. **Writes them to the currency's folder**, `config/<product>/` (such as `config/DOGE-USD/`), as `<prefix>_###.json`, under the next numbers not used by any model anywhere in `config/`, in any discard folder or in the performance log. Names are never reused, so a log row always belongs to one model. A profile identical to one already in `config/` or `config/discard/` isn't written again.
3. **Records them in the performance log**, exactly as the simulator would.
4. **Ranks every model of the currency** and moves the bottom 80% to `config/<product>/discard/`. See [Ranking and the discard folder](#ranking-and-the-discard-folder).
5. **Writes the report**, `data/model_search/<run>/report.md`, which ends with the ranking.

## The settings file

Every key below is required; the search stops with a list of anything missing.

| Section | Key | Meaning |
|---|---|---|
| `market` | `product` | Coinbase product to search, such as `DOGE-USD`. |
| | `prefix` | Output names: `config/<prefix>_###.json`, numbered on from the highest number already used. |
| | `template_profile` | Profile (path from the repository root) whose friction, capital, tax and execution blocks every model copies. |
| | `granularities` | Candle sizes models may use, such as `["ONE_DAY", "SIX_HOUR"]`. |
| | `reference_fallback_granularity` | Candle size given to reference models whose own size isn't in `granularities`. |
| `windows` | `train_start`, `holdout_start` | Dates (`YYYY-MM-DD`). Models are scored and selected on `[train_start, holdout_start)`; the holdout, from `holdout_start` to the latest candle, is only reported. |
| | `validation_start` | Optional date inside the training window. When set, each model is also scored from it to `holdout_start`, and the search selects on the **weaker** of its training and validation fitness, so a model must work in the recent part of the training window too, not just over it as a whole. Breaks the ties of many models with identical training trades, and makes training-only luck less rewarding. |
| `search` | `population` | Models bred per generation. |
| | `keep` | Best models written to `config/` at the end. |
| | `elites` | Best models so far that parents are drawn from. |
| | `generators`, `evaluators` | Worker processes that breed and that backtest (one analyst always runs): a number, or `"auto"` for one per CPU but two, divided between the searches of an `--instances` run. Breeding and backtesting take turns, so both can use every spare CPU. |
| | `batch_size` | Models per evaluator batch. |
| | `min_generations`, `max_generations` | Bounds on the number of generations. |
| | `patience`, `tolerance` | A plateau: neither the best score nor the top-20 mean has gained `tolerance` in `patience` generations. |
| | `seed` | Random seed for the coordinator, or `null` for a different search every run. An `--instances` run gives its searches consecutive seeds. |
| `fitness` | `drawdown_weight` | Score = (log growth + `drawdown_weight` × log(1 − max drawdown)) per year … |
| | `min_trades`, `trade_penalty` | … less `trade_penalty` for each trade short of `min_trades`. |
| | `max_trades` | Optional: also less `trade_penalty` for each trade beyond it, to steer towards low turnover. |

Among models with equal fitness, the search prefers the simpler one (fewer conditions), then the one that trades less.
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

These sections are optional; a settings file without them gets the defaults shown.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `references` | | `null` | Profiles the search learns from: a list of paths or globs from the repository root, such as `["config/doge_*.json"]`. `null` learns from every profile in `config/`. |
| `models_dir` | | `null` | Folder, from the repository root, the search writes its models to. `null` is the currency's folder, `config/<product>/`. |
| `goals` | | `null` | Path of the goals file the ranking uses. `null` uses `search_config/goals.json` when it exists, and otherwise ranks by score alone with the default ranking settings. |
| `variety` | `max_similarity` | `0.8` | The most two kept models' conditions may overlap: the share of their conditions in common, from 0 to 1. Lower keeps more varied models; `1` keeps every model that trades differently. |
| | `require_train_outperformance` | `false` | When `true`, only models whose training-window return strictly beats buy and hold are eligible to be kept. The holdout is not used for this gate. |
| `registry` | `enabled` | `true` | Share evaluations through `data/model_search/registry.db` (see below). |
| | `migrants` | `10` | Other searches' best models brought in as parents each generation; `0` brings none. |
| `ranking` | `enabled` | `true` | Rank the currency's models and move the bottom to its discard folder at the end. |

## Choosing reference profiles and the models folder

By default a search learns from every profile in `config/`, whatever its currency, and writes its models there. Two settings (or the matching options) change that:

- **`references`** points the search at chosen profiles, so you can refine one family of models: `--references "config/doge_a_*.json"`. Their conditions join the condition library, their structures seed the first generation, and their logged results weight what the analyst looks for. A pattern matching nothing stops the search with an error.
- **`models_dir`** gives a search its own folder, such as `--models-dir work/doge_trend`, so experiments don't mix with `config/`. The ranking and the discard folder then work within that folder, and `python model_assessor.py --config-dir work/doge_trend` assesses it. The simulator lists only `config/`; use **Add file...** to open a model from another folder. Model names stay unique across every folder, because they also skip names already in the performance log.

## Running searches in parallel

A search already runs in parallel: its generators and evaluators are separate processes. To search faster still, run several searches on the same computer at once, either with `--instances N` or by starting them yourself in separate terminals. They cooperate rather than collide:

- **A shared registry.** Every search records each model it scores in `data/model_search/registry.db`, and looks candidates up there before backtesting them. A model any search has already scored, now or in an earlier run, is never backtested again under the same conditions. The registry only reuses a result when the market, the windows, the template's costs, the fees, the last candle of each candle size and the backtester's version all match, so a fresh download or a new fee tier starts afresh. It stores each model's results rather than its score, so changing the `fitness` settings rescores the stored results instead of backtesting them again. The search log shows `N already scored` each generation.
- **Migrants.** Each generation, a search brings in the best models the other searches have found (`registry.migrants`) as possible parents, so a good idea found in one spreads to all. The log shows `N migrants`.
- **Different starting points.** Searches launched together with `--instances` start from different reference models and seeds.
- **Safe names and files.** Searches take model names under a lock (`config/.models.lock`), so two never write the same name, and skip profiles another has already written. The performance log and the candle database have their own locks.

`--instances N` refreshes the market data and fees once, starts N searches that each get a share of the CPUs (with `"auto"` workers), prefixes each one's output with its number, and ranks the currency's models once when they have all finished. Each search keeps its own run folder and report.

## Ranking and the discard folder

At the end of a search (or with `python model_assessor.py --product <product> --discard`), every model in `config/` for the search's currency is backtested again under the same conditions, so old and new models compare like with like, and ranked:

1. Models that pass the goals come first.
2. Then the models meeting more of the targets.
3. Then the higher score: annualized log growth plus `drawdown_weight` x log(1 - max drawdown).

Every model of the currency is ranked wherever it sits: in its currency's folder, or still at the top of `config/`. Models that only repeat a better model's trades are discarded first. Then the bottom `discard_fraction` of the rest (80% by default) move to the currency's discard folder, `config/<product>/discard/`. The simulator doesn't list that folder, and the search doesn't learn from the models in it. A few things to know:

- **Every model of the currency is ranked, your own included.** Use `ranking.protect` in the goals file, a list of name patterns such as `["btc_best_*", "btc_macro_horizon"]`, to keep models out of the discard whatever their rank.
- **At least `min_keep` models stay** (10 by default), so a small currency isn't emptied.
- **Nothing is deleted.** The discard folder's `discard_log.csv` records each move: when, the model, its rank out of how many, whether it passed, and the targets it missed. A name already in the folder gets a numbered suffix.
- **To bring a model back:** `python model_assessor.py --restore doge_042`.
- **To see what would move first:** `python model_assessor.py --product DOGE-USD --discard --preview`.
- **To set aside every model that ends below buy and hold:** `python model_assessor.py --below-buy-and-hold` (add `--product DOGE-USD` for one currency, `--preview` to only count them). It backtests each model with the current code and live fees first, then moves those whose full-history result ends below buy and hold, measured from where the model could first buy as in the simulator's comparison, to their currency's discard folder, recording each with its result in `discard_log.csv`. A model that can't be judged (it has no result for its current profile) stays.
- **The holdout is no longer untouched.** The search selects on the training window only, but the ranking uses the full history, holdout included. Treat a model that survives the ranking with the same caution as any model chosen by its backtest.

## Goals and the assessor

A goals file sets the targets a model should meet. Start from the template:

```bash
cp search_config/.example/goals.example.json search_config/goals.json
python model_assessor.py                          # every model in config/, from the performance log
python model_assessor.py --product DOGE-USD       # one currency
python model_assessor.py config/doge_0*.json      # chosen profiles
python model_assessor.py --product DOGE-USD --rerun   # backtest them first, in parallel
python model_assessor.py --config-dir work/doge_trend  # models in another folder
```

The assessor prints a table of the models, ranked as above, with whether each passes, how many targets it meets, its annual growth, ending value, maximum drawdown, trades and score. It writes every detail, including each target met and missed, to `data/assessments/assessment_<time>.csv`. Without `--rerun`, it reads the performance log, and ignores a row whose profile has been edited since. `--rerun` and `--discard` download new candles and look up your fee tier first, like the simulator; add `--offline` to use the cached candles and each profile's fallback fee.

Every target is optional: leave it out or set it to `null` to ignore it.

| Section | Key | A model meets it when |
|---|---|---|
| `growth` | `min_annual_growth_pct` | Its value grew by at least this percentage a year, compounded over the test. |
| | `min_growth_multiple` | Its ending value is at least this multiple of its starting capital. |
| | `min_vs_buy_and_hold_pct` | Its ending value beats buy and hold's by at least this percentage (negative allows trailing it). |
| `ending_value` | `min_ending_value` | Its ending value is at least this amount, in the quote currency. |
| `risk` | `max_drawdown_pct` | Its largest fall from a peak is at most this percentage. |
| | `max_worst_in_window_pct` | Its worst fall within the drawdown window (180 days unless the profile sets one) is at most this percentage. |
| | `min_trades` | It completed at least this many trades. |
| | `min_win_rate_pct` | At least this percentage of its trades made money. |
| | `require` | `"all"` (the default): a model passes when it meets every target set. `"any"`: when it meets at least one. |
| `ranking` | `drawdown_weight` | `0.5`: how heavily the score marks down drawdowns. |
| | `discard_fraction` | `0.8`: the share of a currency's models moved to its discard folder. |
| | `min_keep` | `10`: the fewest models a currency keeps. |
| | `protect` | `[]`: name patterns never discarded, such as `"btc_best_*"`. |

`require` sits at the top level of the file, beside the three target sections.

### A recent-period gate

A model's full-history results include the period it was selected on, so a model that fitted the past well can rank high however it has done lately. A `recent` section adds targets for a recent period alone:

```json
"recent": {"start": "2025-04-01", "min_return_pct": -5, "min_vs_buy_and_hold_ratio": 1.5,
           "max_drawdown_pct": 42, "min_trades": 3, "score_weight": 0.5}
```

| Key | Meaning |
|---|---|
| `start` | The recent period's first day. Every model is backtested from it, with indicators warmed up on the history before, live fees, and nothing written to the performance log. |
| `min_return_pct` | The model's return over the period is at least this percentage. |
| `min_vs_buy_and_hold_ratio` | Its growth is at least this multiple of buy and hold's over the period: 1.5 with buy and hold at 0.5x needs 0.75x or better. |
| `max_drawdown_pct`, `min_trades` | Its drawdown and trade count over the period. |
| `score_weight` | Adds this multiple of the period's score to the model's ranking score (0 by default). |

Recent targets count like the others towards passing and towards the targets met, and the assessor's table gains a `recent` return column. A model that can't run from the start date misses them all.

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

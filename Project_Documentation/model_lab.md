# Model Lab Guide

Model Lab is a desktop window for running model searches yourself: queue up search settings files, run them on this computer (several at once if you like), and browse the top 20 models they find against your goals.

```bash
python model_lab.py
```

The intended workflow keeps an assistant's work to writing files. Have it draft search settings and goals files in `search_config/` (their format is in [search_config/README.md](../search_config/README.md)), then run, rerun and compare them in Model Lab without any further help. Everything Model Lab does runs locally.

---

## Contents

1. [Add searches to the queue](#1-add-searches-to-the-queue)
2. [Run the queue](#2-run-the-queue)
3. [The top 20](#3-the-top-20)
4. [What runs where](#4-what-runs-where)
5. [Troubleshooting](#5-troubleshooting)

---

## 1. Add searches to the queue

**Search settings** lists every runnable settings file in `search_config/`: files with a `market` section that pass the same checks `model_search.py` makes, including that the template profile exists. Below the controls, in red, Model Lab lists any file it can't use and why, such as a missing key or invalid JSON. Click **Reload files** after writing or editing a file.

The line under the controls shows the chosen file's currency, how its models will be named (`<prefix>_###`), and its own goals file.

- **Goals** picks the goals file the search's ranking uses. `(the settings' own goals)` uses the settings' `goals` entry, or `search_config/goals.json` if it sets none. Every valid goals file in `search_config/` is listed.
- **When it ends, rank the currency's models and move the bottom 80% to its discard folder** runs the ranking described in [Ranking and the discard folder](../search_config/README.md#ranking-and-the-discard-folder) after the search. It is off by default, so a search only adds models.
- **Add to queue** adds the search. The same file can be queued several times; each run uses a fresh seed unless the settings fix one.

The **Queue** table shows each search's settings, currency, goals, whether it ranks, its status (`queued`, `running`, `done`, `failed` or `stopped`), how long it took, and a note, such as the last line a failed search printed.

- **Move up** and **Move down** reorder the selected searches.
- **Remove** takes them out (not while running).
- **Run again** queues finished, failed or stopped searches again.
- **Clear finished** removes everything that isn't queued or running.

The queue is saved in `data/model_lab/queue.json` and comes back when Model Lab reopens. A search that was running when Model Lab closed is queued again.

## 2. Run the queue

- **Searches at once** sets how many queued searches run at the same time. The CPUs Model Lab may use (one per CPU but two, as in the simulator) are shared between them, and the line beside it says how many generator and evaluator workers each search gets. One search at a time gives each search every worker; several at once explores more variety, and searches on the same currency also share results through the [registry](../search_config/README.md#running-searches-in-parallel).
- **Offline** runs searches on cached candles with each profile's fallback fee. It understates fees against your live tier, so Model Lab asks before starting; treat offline results as previews.
- **Start queue** runs the queued searches in order. **Stop** ends the running searches and their worker processes; they are marked `stopped`, and searches still queued stay queued.

Each search's output appears in the **Log** tab, prefixed with its settings file's name, as it runs. When a search finishes, the Top 20 tab updates for its currency.

## 3. The top 20

The **Top 20** tab ranks a currency's models with the [assessor](../search_config/README.md#goals-and-the-assessor) and lists the 20 best, counting models that trade identically as one.

- **Currency** and **Goals** choose what to rank and against which goals file. `(no goals: rank by score)` ranks by risk-adjusted growth alone.
- **All models of the currency** ranks every model in its folder; **Only this queue's new models** ranks only the models written since the queue started.
- **Assess** ranks again with the current choices. After each finished search it runs on its own, with that search's currency and goals.
- The columns are the rank, model, currency, whether it passes the goals, how many targets it meets, annual growth, ending value, growth multiple, maximum drawdown, trades, the return over the goals' recent period (when the goals set one), the score, and the targets it missed.
- **Open selected in simulator**, or double-clicking a row, opens the simulator with those models selected, ready to run and chart.

Results come from the performance log, where every search records its models. With a [recent-period gate](../search_config/README.md#a-recent-period-gate) in the goals, every model of the currency is also backtested from its start date, which takes longer for a large currency.

## 4. What runs where

Each queued search is `python model_search.py --settings <file>` with the chosen goals, ranking and worker counts, run as a separate process. Its models go to the currency's folder, `config/<product>/`; its report goes to `data/model_search/<run>/report.md`; and its results go to `data/model_performance.csv`. Searches download new candles and look up your fee tier first, so the `.env` API key must be set, as for the simulator.

## 5. Troubleshooting

| Problem | What to do |
|---|---|
| A settings file isn't in the list | It's listed in red with the reason. Fix it, then click **Reload files**. |
| A search fails at once | Its last output line is in the Note column, and the full output is in the Log tab. A missing API key or network error shows there. |
| The top 20 is empty with "Only this queue's new models" | No search in this session has written models for that currency yet, or every new model was a duplicate of an existing one. |
| The computer slows down | Run fewer searches at once. |

# scenarios/: synthetic market scenario sets

Scenario sets for the simulator's synthetic testing (**Test options...** in the simulator window; see the [Simulator guide](../Project_Documentation/simulator.md#synthetic-scenarios)). Each file is one set:

```json
{"scenarios": [
  {"name": "Crash -50% in a week, half recovered in 6 months", "type": "crash", "placement": "append",
   "drop": 0.5, "days": 7, "recovery_days": 180, "recovery": 0.5, "volatility": 1.0, "seed": 1}
]}
```

`stress_test.json` is written with example scenarios the first time the simulator needs it, including a chained run (a crash, then chop, then a calm rally). Edit sets in the Test options window, or by hand.

## Scenario types

| Type | What happens | Parameters |
|---|---|---|
| `crash` | Falls `drop` over `days`, then regains `recovery` of the lost price over `recovery_days` (-50% then half recovered ends at -25%) | `drop`, `days`, `recovery_days`, `recovery`, `volatility` |
| `bear` | Grinds down `drop` over `days` | `drop`, `days`, `volatility` |
| `pump` | Rises `rise` over `days`, then falls `drop` over `collapse_days`. With a low `volatility`, a calm rally | `rise`, `days`, `drop`, `collapse_days`, `volatility` |
| `sideways` | Swings about `range` either side of the start for `days` | `range`, `days`, `volatility` |
| `volatility` | Moves like the market's own history, `volatility` times as wild, for `days` | `days`, `volatility` |
| `resample` | `paths` random paths of `days`, each stitched from random blocks of `block_days` of real history | `days`, `paths`, `block_days` |

Every scenario also has:

- `name`: shown in the results.
- `placement`: `append` continues after the last real candle; `at` replaces history from the date `at` (`YYYY-MM-DD`) for the scenario's length and shifts the history after it to continue from where the scenario ended.
- `seed`: the same seed always gives the same candles; change it for a different draw.
- `chain`: `true` makes the scenario continue the run of the scenario above it, so modules play one after another as one path (its own placement is then ignored: the run's first module decides).
- `volatility`: noise relative to the market's own (1 = as noisy as its history, 0 = a smooth path). The noise wanders around the scenario's path without drifting from it, so a scenario ends where it says.

`stress_plan.json` is the stress test plan: the combinations every stress test runs, each a self-contained list of modules played one after another. It is written with the built-in baseline (33 combinations) the first time, then kept as you edit it in the Test options window's **Stress test plan** tab.

`nightmares.json` holds the stress test's nightmare scenarios (see the [Simulator guide](../Project_Documentation/simulator.md#stress-tests)); it is written with five the first time a stress test needs it, and can be edited like any set.

Synthetic candles follow the market's own behaviour at the model's candle size: its typical move per candle, its typical high-low range and its median volume. Models are measured from the scenario's first candle, with indicators warmed up on the real history before it.

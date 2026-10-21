"""Market regimes: switch a model's rules and risk settings by the situation it detects.

A profile may add a "regimes" block. Each regime names a condition (`when`, a rule formula over
the profile's indicators) and what changes while it is active: its own entry and exit signals,
stops, profit target and position size. Anything a regime leaves out comes from the base
profile, which is also the fallback regime ("base") when no condition matches.

    "regimes": {
      "confirm": "7d",          detection lag: a new regime must be the match for this long
      "min_hold": "30d",        once active, a regime stays at least this long
      "on_switch": "keep",      "keep" an open position (the new regime's exits apply) or "close" it
      "list": [
        {"name": "trend", "when": "close > lock * 1.25",
         "exit_signal": {"any": ["votes <= 0"], "trailing_stop": 0},
         "safeguards": {"use_hard_stop": false}},
        {"name": "chop", "when": "adx.adx < 20",
         "entry_signal": {"all": ["rsi < 30"]}, "exit_signal": {"any": ["rsi > 55"]},
         "safeguards": {"use_hard_stop": true, "hard_stop_percentage": 0.08}}
      ]
    }

Detection runs on each candle's close and only on past candles, so it lags: the first regime in
the list whose `when` holds is the candidate, a candidate must stay the same for `confirm`
before it becomes active, and the active regime then holds for at least `min_hold`. The fast
protections (stops, trailing stops, flash-crash buys) still act inside every candle.

Regime-specific settings: entry_signal, exit_signal (rules plus trailing_stop, profit_lock_target,
profit_lock_trail, hard_stop_trigger), safeguards (use_hard_stop, hard_stop_percentage) and
capital_allocation (target_position_fraction). Everything else (indicators, friction, flash-crash
buys, the drawdown limit, order limits) is shared by every regime.
"""

import numpy as np

from indicators import IndicatorError, parse_period
from rules import evaluate_rules, rules_ready

ON_SWITCH = ("keep", "close")
REGIME_KEYS = {"name", "when", "entry_signal", "exit_signal", "safeguards", "capital_allocation", "description"}
EXIT_SETTINGS = ("trailing_stop", "profit_lock_target", "profit_lock_trail", "hard_stop_trigger")
SAFEGUARD_SETTINGS = ("use_hard_stop", "hard_stop_percentage")
ALLOCATION_SETTINGS = ("target_position_fraction",)


def settings_of(profile_like, base=None):
    """The per-regime trading settings of a profile (or a regime, filled in from `base`)."""
    base = base or {}
    exit_signal = profile_like.get("exit_signal", profile_like.get("strategy", {}).get("exit_signal", {})) or {}
    safeguards = profile_like.get("safeguards", {}) or {}
    allocation = profile_like.get("capital_allocation", {}) or {}
    pick = lambda block, key, default: block[key] if key in block else base.get(key, default)  # noqa: E731
    return {
        "trailing_stop": float(pick(exit_signal, "trailing_stop", 0.0) or 0.0),
        "profit_lock_target": float(pick(exit_signal, "profit_lock_target", 0.0) or 0.0),
        "profit_lock_trail": float(pick(exit_signal, "profit_lock_trail", 0.0) or 0.0),
        "hard_stop_trigger": bool(pick(exit_signal, "hard_stop_trigger", True)),
        "use_hard_stop": bool(pick(safeguards, "use_hard_stop", False)),
        "hard_stop_percentage": float(pick(safeguards, "hard_stop_percentage", 0.0) or 0.0),
        "target_position_fraction": float(pick(allocation, "target_position_fraction", 1.0)),
    }


class Regimes:
    """A profile's compiled regimes. Build with compile_regimes(); None when the profile has none."""

    def __init__(self, names, when, entries, exits, settings, confirm, min_hold, on_switch):
        self.names = names            # "base" first, then the listed regimes in order
        self.when = when              # compiled `when` blocks for the listed regimes
        self.entries = entries        # compiled entry blocks, one per name (base first)
        self.exits = exits            # compiled exit blocks, one per name; empty blocks mean no exit rules
        self.settings = settings      # settings_of() per name
        self.confirm = confirm        # candles a candidate must persist before it activates
        self.min_hold = min_hold      # candles an active regime holds at least
        self.on_switch = on_switch

    def summary(self):
        """Each regime's detector, rules and settings as written, for reports: [{name, when, entry_all, ...}]."""
        text = lambda block, key: [rule.text for rule in block[key]]  # noqa: E731
        return [{"name": name, "when": text(self.when[number - 1], "all")[0] if number and self.when[number - 1]["all"]
                 else None,
                 "entry_all": text(self.entries[number], "all"), "entry_any": text(self.entries[number], "any"),
                 "exit_all": text(self.exits[number], "all"), "exit_any": text(self.exits[number], "any"),
                 "settings": self.settings[number]}
                for number, name in enumerate(self.names)]

    def active(self, series, length):
        """The active regime's index (into names) on each candle, after the detection lag."""
        candidate = np.zeros(length, dtype=np.int64)
        # Until every detector has warmed up the situation can't be judged, so no regime is a candidate.
        undecided = np.ones(length, dtype=bool)
        for block in self.when:
            undecided &= rules_ready(block, series, length)
        for number, block in enumerate(self.when, start=1):
            matched = evaluate_rules(block, series, length) & rules_ready(block, series, length) & undecided
            candidate[matched] = number
            undecided &= ~matched
        active = np.zeros(length, dtype=np.int64)
        # The first switch waits only for `confirm`: the history before any switch counts as held.
        current, held, run_value, run_length = 0, self.min_hold, -1, 0
        for index in range(length):
            value = candidate[index]
            run_length = run_length + 1 if value == run_value else 1
            run_value = value
            held += 1
            if value != current and run_length >= self.confirm and held >= self.min_hold:
                current, held = value, 0
            active[index] = current
        return active

    def signals(self, series, length):
        """(active, entry, exit, ready): each candle's regime and its own entry/exit signal, and where the
        active regime's entry rules and every regime condition can be evaluated."""
        active = self.active(series, length)
        entry = np.zeros(length, dtype=bool)
        exit_signal = np.zeros(length, dtype=bool)
        # The model can't tell which regime it is in until every detector has warmed up, so it doesn't enter
        # before then (and buy and hold is measured from the same point, as for any model's warm-up).
        ready = np.ones(length, dtype=bool)
        for block in self.when:
            ready &= rules_ready(block, series, length)
        entry_ready = np.zeros(length, dtype=bool)
        for number in range(len(self.names)):
            mask = active == number
            if not mask.any():
                continue
            entry[mask] = evaluate_rules(self.entries[number], series, length)[mask]
            if self.exits[number]["all"] or self.exits[number]["any"]:
                exit_signal[mask] = evaluate_rules(self.exits[number], series, length)[mask]
            entry_ready[mask] = rules_ready(self.entries[number], series, length)[mask]
        return active, entry & ready, exit_signal, ready & entry_ready


def compile_regimes(profile, compile_block, known, granularity_seconds, errors):
    """The profile's Regimes, or None without a "regimes" block. `compile_block(block, known, label, errors,
    allow_empty)` is CompiledModel._compile; problems are appended to `errors`."""
    block = profile.get("regimes")
    if block is None:
        return None
    if not isinstance(block, dict) or not isinstance(block.get("list"), list) or not block["list"]:
        errors.append('"regimes" must be an object with a non-empty "list" of regimes')
        return None
    strategy = profile.get("strategy", {})
    periods = {}
    for key, default in (("confirm", 1), ("min_hold", 1)):
        try:
            periods[key] = max(1, parse_period(block.get(key, default), granularity_seconds, f"regimes.{key}"))
        except IndicatorError as exc:
            errors.append(str(exc))
            periods[key] = 1
    on_switch = block.get("on_switch", "keep")
    if on_switch not in ON_SWITCH:
        errors.append(f'regimes.on_switch must be "keep" or "close", not {on_switch!r}')
    base_settings = settings_of(profile)
    names, when = ["base"], []
    entries = [compile_block(strategy.get("entry_signal", {}), known, "strategy.entry_signal", [], False)]
    exits = [compile_block(strategy.get("exit_signal", {}), known, "strategy.exit_signal", [], True)]
    settings = [base_settings]
    for number, regime in enumerate(block["list"], start=1):
        if not isinstance(regime, dict):
            errors.append(f"regimes.list[{number}] must be an object")
            continue
        name = regime.get("name") or f"regime_{number}"
        label = f"regimes.{name}"
        unknown = set(regime) - REGIME_KEYS
        if unknown:
            errors.append(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
        if name in names:
            errors.append(f"{label}: regime names must be unique")
        if not regime.get("when"):
            errors.append(f"{label} needs a \"when\" rule")
        names.append(name)
        when.append(compile_block({"all": [regime["when"]]} if regime.get("when") else {}, known, f"{label}.when",
                                  errors, True))
        entry_block = regime.get("entry_signal", strategy.get("entry_signal", {}))
        exit_block = regime.get("exit_signal", strategy.get("exit_signal", {}))
        entries.append(compile_block(entry_block, known, f"{label}.entry_signal", errors, False))
        exits.append(compile_block(exit_block, known, f"{label}.exit_signal", errors, True))
        own = settings_of({"exit_signal": {k: v for k, v in (regime.get("exit_signal") or {}).items() if k in EXIT_SETTINGS},
                           "safeguards": {k: v for k, v in (regime.get("safeguards") or {}).items()
                                          if k in SAFEGUARD_SETTINGS},
                           "capital_allocation": {k: v for k, v in (regime.get("capital_allocation") or {}).items()
                                                  if k in ALLOCATION_SETTINGS}}, base_settings)
        for key in ("trailing_stop", "profit_lock_trail"):
            if not 0 <= own[key] < 1:
                errors.append(f"{label}: {key} must be from 0 up to (not including) 1")
        if own["profit_lock_trail"] and not own["profit_lock_target"]:
            errors.append(f"{label}: profit_lock_trail needs a profit_lock_target")
        settings.append(own)
    return Regimes(names, when, entries, exits, settings, periods["confirm"], periods["min_hold"], on_switch)

"""Compile a model profile's indicators and signal rules into per-candle signals.

A profile defines named indicators (see indicators.py) and writes its entry and exit
signals as rule formulas over them (see rules.py):

    "indicators": {"fast_ema": {"type": "EMA", "period": 30},
                   "slow_ema": {"type": "EMA", "period": 180}},
    "strategy": {"entry_signal": {"all": ["fast_ema > slow_ema"]},
                 "exit_signal": {"any": ["fast_ema < slow_ema"], "profit_lock_target": 0.08}}

Profiles written for the original fixed EMA fields still load: normalize_profile
translates them into this form.
"""

import copy
import keyword

from indicators import (
    INDICATORS,
    PRICE_FIELDS,
    IndicatorContext,
    PANEL_GUIDES,
    IndicatorError,
    output_names,
    panel_outputs,
    price_scale_outputs,
    resolve_parameters,
)
from rules import FUNCTIONS, Rule, RuleError, evaluate_rules

LEGACY_INDICATOR_KEYS = ("fast_window_periods", "slow_window_periods", "emergency_ema_periods")


class ProfileError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("The profile has problems:\n- " + "\n- ".join(errors))


def is_legacy_profile(profile):
    return any(key in profile.get("indicators", {}) for key in LEGACY_INDICATOR_KEYS)


def normalize_profile(profile):
    """Return the profile in the current format, translating the original fixed EMA fields."""
    if not is_legacy_profile(profile):
        return profile
    profile = copy.deepcopy(profile)
    legacy = profile["indicators"]
    source = legacy.get("price_source", "close")
    profile["indicators"] = {
        "fast_ema": {"type": "EMA", "period": legacy.get("fast_window_periods", 30), "source": source},
        "slow_ema": {"type": "EMA", "period": legacy.get("slow_window_periods", 180), "source": source},
        "emergency_ema": {"type": "EMA", "period": legacy.get("emergency_ema_periods", 4), "source": source},
    }
    strategy = profile.setdefault("strategy", {})
    entry = strategy.setdefault("entry_signal", {})
    if "all" not in entry and "any" not in entry:
        rules = []
        if entry.get("fast_ema_gt_slow_ema", True):
            rules.append("fast_ema > slow_ema")
        if entry.get("emergency_ema_gt_fast_ema", False):
            rules.append("emergency_ema > fast_ema")
        rules.append(f"(fast_ema - slow_ema) / slow_ema >= {entry.get('minimum_trend_strength', 0.0)}")
        entry["all"] = rules
    exit_signal = strategy.setdefault("exit_signal", {})
    if "all" not in exit_signal and "any" not in exit_signal:
        exit_signal["any"] = ["fast_ema < slow_ema"] if exit_signal.get("fast_ema_lt_slow_ema", True) else []
    return profile


def _check_indicator_name(name):
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        return f"Indicator name {name!r} must be letters, digits and underscores, not starting with a digit"
    if name in PRICE_FIELDS or name in FUNCTIONS or name in ("True", "False"):
        return f"Indicator name '{name}' is reserved; pick another name"
    return None


def _label(name, definition, parameters):
    if definition.get("label"):
        return definition["label"]
    detail = parameters.get("period") or ", ".join(
        str(parameters[key]) for key in ("fast", "slow", "signal") if key in parameters
    )
    return f"{name} ({definition['type']}{' ' + str(detail) if detail else ''})"


class CompiledModel:
    """A validated profile, ready to produce indicator series and entry/exit signals."""

    def __init__(self, profile, granularity_seconds):
        self.profile = normalize_profile(profile)
        self.granularity_seconds = granularity_seconds
        errors = []

        definitions = self.profile.get("indicators", {})
        if not isinstance(definitions, dict):
            raise ProfileError(["\"indicators\" must be an object mapping names to indicator definitions"])
        self.definitions = {}
        self.parameters = {}
        for name, definition in definitions.items():
            problem = _check_indicator_name(name)
            if problem is None and not isinstance(definition, dict):
                problem = f"Indicator '{name}' must be an object such as {{\"type\": \"EMA\", \"period\": 20}}"
            if problem is None:
                try:
                    self.parameters[name] = resolve_parameters(name, definition, granularity_seconds)
                    self.definitions[name] = definition
                except IndicatorError as exc:
                    problem = str(exc)
            if problem:
                errors.append(problem)

        self.series_names = list(PRICE_FIELDS) + output_names(self.definitions)
        errors += self._check_sources()
        # Rules may still name an indicator that failed validation; its own error already covers it.
        known = list(self.series_names)
        for name, definition in definitions.items():
            if name not in self.definitions:
                indicator_type = definition.get("type") if isinstance(definition, dict) else None
                outputs = INDICATORS[indicator_type][2] if indicator_type in INDICATORS else None
                known += [f"{name}.{field}" for field in outputs] if outputs else [str(name)]
        strategy = self.profile.get("strategy", {})
        self.entry_rules = self._compile(strategy.get("entry_signal", {}), known, "strategy.entry_signal",
                                         errors, allow_empty=False)
        self.exit_rules = self._compile(strategy.get("exit_signal", {}), known, "strategy.exit_signal",
                                        errors, allow_empty=True)
        if errors:
            raise ProfileError(errors)

    def _check_sources(self):
        """Every source must be a price field or another indicator's output, with no loops."""
        errors, depends_on = [], {}
        for name, definition in self.definitions.items():
            source = self.parameters[name].get("source")
            if source is None or source in PRICE_FIELDS:
                continue
            if source not in self.series_names:
                hint = ""
                if source in self.definitions:
                    hint = f"; '{source}' has several outputs, so use one of: " + ", ".join(
                        output_names({source: self.definitions[source]}))
                errors.append(f"Indicator '{name}' has source '{source}', which is not a price field or "
                              f"indicator output{hint}")
                continue
            depends_on[name] = source.split(".", 1)[0]
        for start in depends_on:
            path, current = [start], depends_on.get(start)
            while current is not None:
                if current in path:
                    loop = path[path.index(current):] + [current]
                    message = "Indicator sources form a loop: " + " -> ".join(loop)
                    if min(loop) == start and message not in errors:  # report each loop once
                        errors.append(message)
                    break
                path.append(current)
                current = depends_on.get(current)
        return errors

    def _compile(self, block, known, label, errors, allow_empty):
        """Compile a signal's "all" and "any" rule lists, collecting every problem."""
        compiled = {"all": [], "any": []}
        for key in compiled:
            rules = block.get(key, [])
            if isinstance(rules, str):
                rules = [rules]
            if not isinstance(rules, list):
                errors.append(f"{label}.{key} must be a list of rule formulas")
                continue
            for rule in rules:
                if not isinstance(rule, str):
                    errors.append(f"{label}.{key} entries must be formulas in quotes, not {rule!r}")
                    continue
                try:
                    compiled[key].append(Rule(rule, known, self.granularity_seconds))
                except RuleError as exc:
                    errors.append(str(exc))
        if not allow_empty and not block.get("all") and not block.get("any"):
            errors.append(f"{label} needs at least one rule in \"all\" or \"any\"")
        return compiled

    def evaluate(self, candles):
        """Return (series by name, entry signal array, exit signal array) for the candles."""
        context = IndicatorContext(candles, self.definitions, self.granularity_seconds)
        series = {name: context.series(name) for name in self.series_names}
        entry = evaluate_rules(self.entry_rules, series, len(candles))
        exit_signal = (evaluate_rules(self.exit_rules, series, len(candles))
                       if self.exit_rules["all"] or self.exit_rules["any"] else None)
        return series, entry, exit_signal

    def indicator_meta(self):
        """Per-indicator labels and outputs, for charts, tooltips and reports."""
        meta = []
        for name, definition in self.definitions.items():
            outputs = output_names({name: definition})
            meta.append({
                "name": name,
                "type": definition["type"],
                "label": _label(name, definition, self.parameters[name]),
                "period": self.parameters[name].get("period"),
                "outputs": outputs,
                "price_scale_outputs": price_scale_outputs(name, definition),
                "panel_outputs": panel_outputs(name, definition),
                "levels": PANEL_GUIDES.get(definition["type"], {}).get("levels", []),
                "bounds": PANEL_GUIDES.get(definition["type"], {}).get("bounds"),
                "summary": INDICATORS[definition["type"]][4],
            })
        return meta

    def rule_text(self):
        return {
            "entry_all": [rule.text for rule in self.entry_rules["all"]],
            "entry_any": [rule.text for rule in self.entry_rules["any"]],
            "exit_all": [rule.text for rule in self.exit_rules["all"]],
            "exit_any": [rule.text for rule in self.exit_rules["any"]],
        }


def validate_profile(profile, granularity_seconds):
    """Return a list of problems with the profile's indicators and rules (empty if none)."""
    try:
        CompiledModel(profile, granularity_seconds)
    except ProfileError as exc:
        return exc.errors
    return []

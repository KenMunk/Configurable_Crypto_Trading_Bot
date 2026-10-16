"""Evolutionary model search: breed vote-based models for one market from the reference models.

Everything that shapes a search lives in a settings file, not in this module:
search_config/model_search.json by default. The search_config/ folder is private and
gitignored, like config/. The file names the market and test windows, the conditions
models are built from, the choices for every setting, the fitness, and the breeding and
analysis parameters (see search_config/README.md). Start from the public template:

    cp search_config/.example/model_search.example.json search_config/model_search.json
    python model_search.py                       # or: --settings path/to/settings.json

The existing profiles in config/ and their rows in data/model_performance.csv are the point
of reference. Their rules are split into "atoms" (single conditions such as `close > sma_50d`)
and settings (vote thresholds, persistence windows, the regime lock, stops), and the
characteristics of the best of them seed the search. Each generation then:

  1. generator workers breed candidates (mutation and crossover of the best models so far,
     plus fresh models drawn from the characteristics that have been working),
  2. the evaluator worker backtests them in batches on a training window and an untouched
     holdout window,
  3. the analyst worker measures which characteristics the best models share and feeds
     those weights back to the generators.

The search stops when the best models stop improving (a plateau). It then writes the best
models that each trade differently, and aren't near-copies of one another, as
config/<prefix>_###.json under the next unused numbers, records them in the performance log
exactly as the simulator would, and leaves a report in data/model_search/<run>/. Finally it
ranks every model of the currency in config/ against your goals (see model_assessor.py) and
moves the bottom 80% to config/discard/.

Several searches can run at once on one computer, started separately or with --instances:
they share data/model_search/registry.db, so no model is backtested twice under the same
conditions and each borrows the others' best models as parents, and they take model names
under a lock, so none overwrites another's.

Selection only ever sees the training window; the holdout columns are an honest check.
"""

import argparse
import ast
import csv
import itertools
import json
import math
import multiprocessing as mp
import os
import queue
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backtest import GRANULARITY_SECONDS
from file_lock import file_lock

ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"
SETTINGS_DIR = ROOT / "search_config"
DEFAULT_SETTINGS = SETTINGS_DIR / "model_search.json"
EXAMPLE_SETTINGS = SETTINGS_DIR / ".example" / "model_search.example.json"
SEARCH_DIR = ROOT / "data" / "model_search"

# Every key a settings file must define; see search_config/README.md for what each does.
REQUIRED_SETTINGS = {
    "market": ["product", "prefix", "template_profile", "granularities", "reference_fallback_granularity"],
    "windows": ["train_start", "holdout_start"],
    "search": ["population", "keep", "elites", "generators", "evaluators", "batch_size", "min_generations",
               "max_generations", "patience", "tolerance", "seed"],
    "fitness": ["drawdown_weight", "min_trades", "trade_penalty"],
    "breeding": ["mutate", "crossover", "tournament_size", "max_atoms", "confirm_sizes", "mutations_per_child",
                 "neighbor_span"],
    "analysis": ["top_fraction", "shrink", "weight_floor", "weight_cap", "reference_half_weight_models",
                 "report_min_support", "report_min_atom_support", "reference_min_support"],
    "options": ["entry_n", "exit_n", "entry_ratio", "exit_ratio", "exit_mode", "confirm", "entry_persist_h",
                "exit_persist_h", "lock", "trail", "stop", "target", "dd_limit", "pos"],
}
# Settings a genome carries, each drawn from settings["options"].
GENOME_SETTINGS = ["entry_persist_h", "exit_persist_h", "lock", "trail", "stop", "target", "target_trail",
                   "dd_limit", "pos"]
# Genome options a settings file may leave out, with the choices they then have.
OPTIONAL_OPTIONS = {"target_trail": [0]}


class SettingsError(ValueError):
    pass


def load_settings(path):
    """Read and check a settings file. Raises SettingsError naming everything missing."""
    path = Path(path)
    if not path.exists():
        raise SettingsError(f"No search settings at {path}. Copy the template to start:\n"
                            f"    cp {EXAMPLE_SETTINGS.relative_to(ROOT).as_posix()} "
                            f"{DEFAULT_SETTINGS.relative_to(ROOT).as_posix()}")
    with path.open(encoding="utf-8") as handle:
        settings = json.load(handle)
    missing = [f"{section}.{key}" for section, keys in REQUIRED_SETTINGS.items()
               for key in keys if key not in settings.get(section, {})]
    if not settings.get("atoms"):
        missing.append("atoms")
    unknown = [g for g in settings.get("market", {}).get("granularities", []) if g not in GRANULARITY_SECONDS]
    problems = ([f"missing {', '.join(missing)}"] if missing else []) + (
        [f"unknown granularities {', '.join(unknown)}"] if unknown else [])
    if problems:
        raise SettingsError(f"{path}: " + "; ".join(problems))
    for key, choices in OPTIONAL_OPTIONS.items():
        settings["options"].setdefault(key, choices)
    return settings


def candle_hours(granularity):
    return GRANULARITY_SECONDS[granularity] / 3600


# --- atoms: single bullish conditions with the indicators they need ------------------

def _clean(value):
    return re.sub(r"[^0-9a-zA-Z]", "p", str(value).replace("-", "m"))


def canonical_name(definition):
    """A stable indicator name built from its type and parameters, e.g. sma_200d."""
    parts = [definition["type"].lower()]
    for key in sorted(k for k in definition if k not in ("type", "description", "label", "source")):
        parts.append(_clean(definition[key]))
    if definition.get("source", "close") != "close":
        parts.append("of_" + definition["source"])
    return "_".join(parts)


def indicator(type_, **params):
    definition = {"type": type_, **params}
    return canonical_name(definition), definition


class _Keep(dict):
    """Leaves unknown {placeholders} in place, for a second formatting pass."""

    def __missing__(self, key):
        return "{" + key + "}"


def _fill(value, params):
    """Substitute grid values into a spec; a string that is exactly "{name}" takes the raw value."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(\w+)\}", value)
        if whole and whole[1] in params:
            return params[whole[1]]
        return value.format_map(_Keep(params))
    if isinstance(value, dict):
        return {key: _fill(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, params) for item in value]
    return value


def expand_atoms(specs):
    """The atom library from the settings' "atoms" specs.

    Each spec has a `family`, an `expr` and optional `indicators` written with {placeholders};
    every combination of its `cases` and its `grid` values gives one atom. Indicator
    placeholders in `expr` (and in another indicator's `source`) become canonical names.
    """
    atoms = {}
    for spec in specs:
        grid = spec.get("grid", {})
        keys = list(grid)
        for case in spec.get("cases") or [{}]:
            for values in itertools.product(*(grid[key] for key in keys)):
                params = {**case, **dict(zip(keys, values))}
                definitions = _fill(spec.get("indicators", {}), params)
                names = {}
                for _ in range(len(definitions) + 1):
                    for key, definition in definitions.items():
                        source = re.fullmatch(r"\{(\w+)\}", str(definition.get("source", "")))
                        if source:
                            if source[1] not in names:
                                continue
                            definition["source"] = names[source[1]]
                        names[key] = canonical_name(definition)
                if len(names) != len(definitions):
                    raise SettingsError(f"atom family {spec.get('family')!r}: an indicator source never resolves")
                expr = _fill(spec["expr"], params).format(**names)
                if expr not in atoms:
                    atoms[expr] = {"expr": expr, "family": _fill(spec["family"], params).lower(),
                                   "inds": {names[key]: definition for key, definition in definitions.items()},
                                   "origin": "library"}
    return list(atoms.values())


# --- harvesting the reference models -------------------------------------------------

class _Rename(ast.NodeTransformer):
    def __init__(self, mapping):
        self.mapping = mapping

    def visit_Call(self, node):
        node.args = [self.visit(arg) for arg in node.args]  # never rename the function itself
        return node

    def visit_Name(self, node):
        if node.id in self.mapping:
            node.id = self.mapping[node.id]
        return node


def _flatten_sum(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_sum(node.left) + _flatten_sum(node.right)
    return [node]


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _const(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    return None


def _persist_hours(node):
    """`lowest(X, 'Nh') >= 1` gives (X, N hours); anything else (node, 0)."""
    if (isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.GtE) and _const(node.comparators[0]) == 1
            and isinstance(node.left, ast.Call) and getattr(node.left.func, "id", "") == "lowest"
            and len(node.left.args) == 2 and isinstance(node.left.args[1], ast.Constant)):
        match = re.fullmatch(r"(\d+)([hd])", str(node.left.args[1].value))
        if match:
            return node.left.args[0], int(match[1]) * (24 if match[2] == "d" else 1)
    return node, 0


def _vote(node):
    """A comparison of a sum of conditions with a number: (atom nodes, operator, k)."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return None
    k = _const(node.comparators[0])
    terms = _flatten_sum(node.left)
    if k is None or not all(isinstance(t, ast.Compare) for t in terms):
        return None
    return terms, type(node.ops[0]).__name__, k


class Harvest:
    """Atoms and genomes recovered from the reference profiles."""

    def __init__(self, library, granularities, fallback_granularity):
        self.atoms = {atom["expr"]: atom for atom in library}
        self.granularities, self.fallback = granularities, fallback_granularity
        self.genomes = []  # (name, genome)
        self.skipped = []

    def _canonical_atom(self, node, indicators, mapping):
        node = _Rename(mapping).visit(ast.parse(ast.unparse(node), mode="eval").body)
        expr = ast.unparse(node)
        if expr not in self.atoms:
            used = {}
            for name in _names_in(node):
                for original, canonical in mapping.items():
                    if canonical == name:
                        used[canonical] = indicators[original]
            # Indicators fed by another indicator need that one too.
            for definition in list(used.values()):
                source = definition.get("source")
                if source and source in used:
                    continue
                for original, canonical in mapping.items():
                    if canonical == source:
                        used[canonical] = indicators[original]
            types = sorted({d["type"].lower() for d in used.values()}) or ["price"]
            self.atoms[expr] = {"expr": expr, "family": "ref_" + "_".join(types), "inds": used,
                                "origin": "reference"}
        return expr

    def add_profile(self, name, profile):
        indicators = {}
        mapping = {}
        raw = profile.get("indicators", {})
        # Resolve sources first so derived indicators name their canonical source.
        for _ in range(3):
            for key, definition in raw.items():
                clean = {k: v for k, v in definition.items() if k not in ("description", "label")}
                if "source" in clean:
                    clean["source"] = mapping.get(clean["source"], clean["source"])
                indicators[key] = clean
                mapping[key] = canonical_name(clean)
        strategy = profile.get("strategy", {})
        gran = profile.get("period_granularity", "ONE_DAY")
        # Candle sizes outside the search move to the fallback one.
        gran = gran if gran in self.granularities else self.fallback
        genome = {"gran": gran, "confirm": [], "confirm_k": 0,
                  "lock": 0, "entry_persist_h": 0, "exit_persist_h": 0}
        try:
            terms, persist = [], 0
            for rule in strategy["entry_signal"].get("all", []):
                node, hours = _persist_hours(ast.parse(rule, mode="eval").body)
                persist = max(persist, hours)
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                    terms.extend(node.values)
                else:
                    terms.append(node)
            genome["entry_persist_h"] = persist
            votes = 0
            for term in terms:
                vote = _vote(term)
                if vote and vote[1] in ("GtE", "Gt"):
                    atoms, op, k = vote
                    k = int(k) + (1 if op == "Gt" else 0)
                    exprs = [self._canonical_atom(a, indicators, mapping) for a in atoms]
                    if votes == 0:
                        genome["entry"], genome["entry_k"] = exprs, k
                    else:
                        genome["confirm"], genome["confirm_k"] = exprs, k
                    votes += 1
                elif self._is_lock(term, indicators, after_vote=votes > 0):
                    genome["lock"] = int(str(indicators[term.comparators[0].id]["period"]).rstrip("d"))
                else:
                    expr = self._canonical_atom(term, indicators, mapping)
                    if votes == 0:
                        genome["entry"], genome["entry_k"] = [expr], 1
                        votes += 1
                    else:
                        raise ValueError("unrecognised entry term")
            if "entry" not in genome:
                raise ValueError("no entry vote")
            exit_rules = strategy["exit_signal"].get("any", [])
            if len(exit_rules) != 1:
                raise ValueError("exit is not a single rule")
            node, hours = _persist_hours(ast.parse(exit_rules[0], mode="eval").body)
            genome["exit_persist_h"] = hours
            vote = _vote(node)
            if not vote or vote[1] not in ("LtE", "Lt"):
                raise ValueError("exit is not a vote")
            atoms, op, k = vote
            genome["exit"] = [self._canonical_atom(a, indicators, mapping) for a in atoms]
            genome["exit_k"] = int(k) - (1 if op == "Lt" else 0)
            exit_signal = strategy["exit_signal"]
            safeguards = profile.get("safeguards", {})
            genome["trail"] = float(exit_signal.get("trailing_stop", 0) or 0)
            genome["target"] = float(exit_signal.get("profit_lock_target", 0) or 0)
            genome["target_trail"] = float(exit_signal.get("profit_lock_trail", 0) or 0)
            genome["stop"] = float(safeguards.get("hard_stop_percentage", 0)) if (
                safeguards.get("use_hard_stop") and exit_signal.get("hard_stop_trigger", True)) else 0.0
            genome["dd_limit"] = float(safeguards.get("max_drawdown_limit", 0) or 0)
            genome["pos"] = float(profile.get("capital_allocation", {}).get("target_position_fraction", 1.0))
            if "flash_crash_buy" in strategy:
                raise ValueError("flash-crash buys are outside the search space")
        except (ValueError, KeyError, SyntaxError, TypeError, AttributeError) as exc:
            self.skipped.append((name, str(exc)))
            return None
        genome["exit_mode"] = "same" if sorted(genome["exit"]) == sorted(genome["entry"]) else (
            "confirm" if genome["confirm"] and sorted(genome["exit"]) == sorted(genome["confirm"]) else "own")
        self.genomes.append((name, genome))
        return genome

    @staticmethod
    def _is_lock(term, indicators, after_vote=False):
        """Whether an entry term is the SMA regime lock, `close > <N-day SMA>`: an indicator named
        lock..., or (as build_profile writes it, named like any SMA) such a term after the entry vote."""
        if not (isinstance(term, ast.Compare) and len(term.ops) == 1 and isinstance(term.ops[0], ast.Gt)
                and getattr(term.left, "id", "") == "close" and isinstance(term.comparators[0], ast.Name)):
            return False
        name = term.comparators[0].id
        definition = indicators.get(name, {})
        if definition.get("type") != "SMA" or not re.fullmatch(r"\d+d", str(definition.get("period", ""))):
            return False
        return name.startswith("lock") or after_vote


def profile_path(path):
    """A profile path from the repository root, such as a template_profile. A path written before currency
    folders (config/<name>.json) is found in its currency's folder."""
    candidate = ROOT / path
    if not candidate.exists() and Path(path).parts[:1] == ("config",) and len(Path(path).parts) == 2:
        moved = sorted(ROOT.glob(f"config/*/{Path(path).name}"))
        if moved:
            return moved[0]
    return candidate


def _match(pattern):
    """The profiles a reference pattern (a path or glob from the repository root) names."""
    matched = sorted(ROOT.glob(pattern)) if any(ch in pattern for ch in "*?[") else [ROOT / pattern]
    return [path for path in matched if path.suffix == ".json" and path.exists() and path.name != "best_model.json"
            and "discard" not in path.relative_to(ROOT).parts]


def reference_paths(settings):
    """The profiles a search learns from: those matching the settings' "references" patterns (paths or
    globs from the repository root), or every profile in the library (config/ and each currency's
    folder, see model_library.py) when it sets none."""
    from model_library import profile_paths
    patterns = settings.get("references") or []
    if not patterns:
        return profile_paths(CONFIG_DIR)
    paths = []
    for pattern in patterns:
        matched = _match(pattern)
        if not matched and Path(pattern).parts[:1] == ("config",) and len(Path(pattern).parts) == 2:
            # A path from before currency folders, such as config/doge_061.json: look in each currency's folder.
            matched = _match(f"config/*/{Path(pattern).name}")
        if not matched:
            raise SettingsError(f"references: {pattern!r} matches no profile")
        paths.extend(path for path in matched if path not in paths)
    return paths


def models_dir(settings):
    """Where a search writes its models: the settings' "models_dir", else its currency's folder,
    config/<product>/ (see model_library.py)."""
    if settings.get("models_dir"):
        return ROOT / settings["models_dir"]
    return CONFIG_DIR / settings["market"]["product"]


def load_reference(library, performance_path, settings, paths=None):
    """Harvest the reference profiles (see reference_paths), with their logged performance as a reference score."""
    market = settings["market"]
    harvest = Harvest(library, market["granularities"], market["reference_fallback_granularity"])
    rows = {}
    if performance_path.exists():
        with performance_path.open(newline="", encoding="utf-8") as handle:
            rows = {row["model"]: row for row in csv.DictReader(handle)}
    scored = []
    for path in reference_paths(settings) if paths is None else paths:
        with path.open(encoding="utf-8") as handle:
            profile = json.load(handle)
        if "strategy" not in profile:
            continue
        genome = harvest.add_profile(path.stem, profile)
        row = rows.get(path.stem)
        if genome is not None and row is not None and row.get("buy_and_hold_multiple"):
            scored.append((path.stem, genome, reference_score(row, settings["fitness"])))
    return harvest, scored


def reference_score(row, fitness):
    """Annualized risk-adjusted log growth over buy and hold, from a performance-log row."""
    start = datetime.strptime(row["test_start"], "%Y-%m-%d %H:%M")
    end = datetime.strptime(row["test_end"], "%Y-%m-%d %H:%M")
    years = max((end - start).days / 365.25, 0.5)
    growth, held = float(row["growth_multiple"]), float(row["buy_and_hold_multiple"])
    drawdown = min(float(row["max_drawdown_pct"]) / 100, 0.99)
    return (math.log(max(growth, 1e-6)) + fitness["drawdown_weight"] * math.log(1 - drawdown)
            - math.log(max(held, 1e-6))) / years


# --- genomes to profiles -------------------------------------------------------------

def snap_hours(hours, granularity):
    """Persistence in whole candles; under two candles means none."""
    candle = candle_hours(granularity)
    candles = round(hours / candle) if hours else 0
    return int(candles * candle) if candles >= 2 else 0


def _vote_text(exprs, op, k):
    return "(" + " + ".join(f"({e})" for e in exprs) + f") {op} {k}"


def build_profile(genome, atoms, template, product_id):
    """The model profile JSON for `genome`."""
    gran = genome["gran"]
    entry = sorted(set(genome["entry"]))
    exits = sorted(set(genome["exit"]))
    confirm = sorted(set(genome.get("confirm") or []))
    used = entry + exits + confirm
    indicators = {}
    for expr in used:
        for name, definition in atoms[expr]["inds"].items():
            indicators[name] = dict(definition)
    lock_name = None
    if genome.get("lock"):
        lock_name, definition = indicator("SMA", period=f"{genome['lock']}d")
        indicators[lock_name] = definition
    for name, definition in indicators.items():
        definition["description"] = f"{definition['type']} " + ", ".join(
            f"{k}={v}" for k, v in definition.items() if k != "type")

    entry_k = max(1, min(genome["entry_k"], len(entry)))
    terms = [_vote_text(entry, ">=", entry_k)]
    words = [f"Buy when at least {entry_k} of {len(entry)} signals are true: {'; '.join(entry)}."]
    if confirm:
        confirm_k = max(1, min(genome["confirm_k"], len(confirm)))
        terms.append(_vote_text(confirm, ">=", confirm_k))
        words.append(f"Confirmed by at least {confirm_k} of {len(confirm)}: {'; '.join(confirm)}.")
    if lock_name:
        terms.append(f"close > {lock_name}")
        words.append(f"Only buy above the {genome['lock']}-day SMA.")
    entry_persist = snap_hours(genome.get("entry_persist_h", 0), gran)
    if entry_persist:
        entry_rules = ["lowest((" + " and ".join(f"({t})" for t in terms) + f"), '{entry_persist}h') >= 1"]
        words.append(f"Every buy condition must have held for {entry_persist} hours.")
    else:
        entry_rules = terms

    exit_k = max(0, min(genome["exit_k"], len(exits) - 1))
    exit_rule = _vote_text(exits, "<=", exit_k)
    exit_persist = snap_hours(genome.get("exit_persist_h", 0), gran)
    exit_words = f"Sell when {exit_k} or fewer of {len(exits)} signals remain true: {'; '.join(exits)}."
    if exit_persist:
        exit_rule = f"lowest(({exit_rule}), '{exit_persist}h') >= 1"
        exit_words += f" The sell condition must have held for {exit_persist} hours."

    profile = json.loads(json.dumps(template))
    base = product_id.split("-")[0]
    profile.update({"ticker": base, "product_id": product_id, "period_granularity": gran,
                    "indicators": indicators})
    exit_signal = {"any": [exit_rule], "hard_stop_trigger": bool(genome.get("stop")),
                   "profit_lock_target": genome.get("target", 0) or 0, "description": exit_words}
    if genome.get("trail"):
        exit_signal["trailing_stop"] = genome["trail"]
    if genome.get("target_trail") and genome.get("target"):
        exit_signal["profit_lock_trail"] = genome["target_trail"]
        exit_words += (f" On reaching the {genome['target']:.0%} profit target, it holds on and sells after a "
                       f"{genome['target_trail']:.0%} fall from the high.")
        exit_signal["description"] = exit_words
    profile["strategy"] = {"direction": "LONG_ONLY", "market_regime": "TREND_FOLLOWING",
                           "entry_signal": {"description": " ".join(words), "all": entry_rules},
                           "exit_signal": exit_signal}
    profile.setdefault("capital_allocation", {})["target_position_fraction"] = genome.get("pos", 1.0)
    safeguards = profile.setdefault("safeguards", {})
    safeguards["use_hard_stop"] = bool(genome.get("stop"))
    safeguards["hard_stop_percentage"] = genome.get("stop") or 0.03
    safeguards["max_drawdown_limit"] = genome.get("dd_limit", 0) or 0
    return profile


# --- characteristics (what the analyst measures) -------------------------------------

def _bucket(value, options):
    """The smallest option at or above `value` (the largest, past the end), as text."""
    ordered = sorted(options)
    for option in ordered:
        if value <= option:
            return str(option)
    return str(ordered[-1])


def features(genome, atoms, options):
    """The characteristics of a genome, as 'name=value' strings."""
    gran = genome["gran"]
    entry, exits, confirm = genome["entry"], genome["exit"], genome.get("confirm") or []
    out = [
        f"gran={gran}",
        f"entry_n={len(entry)}",
        f"exit_n={len(exits)}",
        "entry_ratio=" + _bucket(genome["entry_k"] / max(len(entry), 1), options["entry_ratio"]),
        "exit_ratio=" + _bucket(genome["exit_k"] / max(len(exits), 1), options["exit_ratio"]),
        f"exit_mode={genome.get('exit_mode', 'own')}",
        f"confirm={int(bool(confirm))}",
        "entry_persist_h=" + _bucket(snap_hours(genome.get("entry_persist_h", 0), gran), options["entry_persist_h"]),
        "exit_persist_h=" + _bucket(snap_hours(genome.get("exit_persist_h", 0), gran), options["exit_persist_h"]),
    ]
    out += [f"{s}=" + _bucket(genome.get(s) or 0, options[s]) for s in GENOME_SETTINGS if not s.endswith("persist_h")]
    for side, exprs in (("entry", entry), ("exit", exits)):
        out.extend(sorted({f"{side}_family={atoms[e]['family']}" for e in exprs}))
        out.extend(f"{side}_atom={e}" for e in exprs)
    return out


def lifts(records, top_fraction, shrink):
    """How much more often each characteristic appears among the top models than overall.

    `records` is a list of (features, score). Returns {feature: (lift, support, top_count)}.
    Lifts are shrunk towards 1 for rare characteristics.
    """
    if len(records) < 10:
        return {}
    ranked = sorted(records, key=lambda r: r[1], reverse=True)
    top_count = max(10, int(len(ranked) * top_fraction))
    share = top_count / len(ranked)
    support, in_top = Counter(), Counter()
    for index, (feats, _) in enumerate(ranked):
        for feat in set(feats):
            support[feat] += 1
            if index < top_count:
                in_top[feat] += 1
    return {feat: (((in_top[feat] + shrink * share) / (n + shrink)) / share, n, in_top[feat])
            for feat, n in support.items()}


# --- generator worker ----------------------------------------------------------------

class Breeder:
    """Makes candidate genomes. One per generator process."""

    def __init__(self, atoms, template, settings, seed):
        from model_config import validate_profile
        self.atoms, self.template = atoms, template
        self.product_id = settings["market"]["product"]
        self.granularities = settings["market"]["granularities"]
        self.options = {"gran": self.granularities, **settings["options"]}
        self.breeding = settings["breeding"]
        self.random = random.Random(seed)
        self.validate = validate_profile
        self.valid_cache = {}
        self.weights = {}
        self.by_family = defaultdict(list)
        for expr, atom in atoms.items():
            self.by_family[atom["family"]].append(expr)

    def atom_ok(self, expr, gran):
        key = (expr, gran)
        if key not in self.valid_cache:
            genome = {"gran": gran, "entry": [expr], "entry_k": 1, "exit": [expr], "exit_k": 0}
            profile = build_profile(genome, self.atoms, self.template, self.product_id)
            self.valid_cache[key] = not self.validate(profile, GRANULARITY_SECONDS[gran])
        return self.valid_cache[key]

    def w(self, feature):
        return self.weights.get(feature, 1.0)

    def choose(self, setting):
        options = self.options[setting]
        weights = [self.w(f"{setting}={o}") for o in options]
        return self.random.choices(options, weights)[0]

    def pick_atoms(self, side, gran, count, exclude=()):
        pool = [e for e in self.atoms if e not in exclude and self.atom_ok(e, gran)]
        weights = [self.w(f"{side}_atom={e}") * self.w(f"{side}_family={self.atoms[e]['family']}")
                   / math.sqrt(len(self.by_family[self.atoms[e]['family']])) for e in pool]
        chosen = []
        for _ in range(min(count, len(pool))):
            index = self.random.choices(range(len(pool)), weights)[0]
            chosen.append(pool.pop(index))
            weights.pop(index)
        return chosen

    def fresh(self):
        gran = self.choose("gran")
        entry = self.pick_atoms("entry", gran, self.choose("entry_n"))
        genome = {"gran": gran, "entry": entry,
                  "entry_k": max(1, round(len(entry) * self.choose("entry_ratio"))),
                  "confirm": [], "confirm_k": 0, "exit_mode": self.choose("exit_mode")}
        if self.choose("confirm") or genome["exit_mode"] == "confirm":
            confirm = self.pick_atoms("entry", gran, self.random.choice(self.breeding["confirm_sizes"]),
                                      exclude=entry)
            genome["confirm"], genome["confirm_k"] = confirm, max(1, len(confirm) // 2)
        if genome["exit_mode"] == "same":
            genome["exit"] = list(entry)
        elif genome["exit_mode"] == "confirm" and genome["confirm"]:
            genome["exit"] = list(genome["confirm"])
        else:
            genome["exit_mode"] = "own"
            genome["exit"] = self.pick_atoms("exit", gran, self.choose("exit_n"))
        genome["exit_k"] = min(len(genome["exit"]) - 1, round(len(genome["exit"]) * self.choose("exit_ratio")))
        for setting in GENOME_SETTINGS:
            genome[setting] = self.choose(setting)
        return genome

    def mutate(self, parent):
        genome = json.loads(json.dumps(parent))
        most = self.breeding["max_atoms"]
        for _ in range(self.random.choice(self.breeding["mutations_per_child"])):
            op = self.random.choice(["swap_entry", "swap_exit", "add_entry", "drop_entry", "add_exit", "drop_exit",
                                     "entry_k", "exit_k", "setting", "setting", "neighbor", "gran"])
            gran = genome["gran"]
            if op == "swap_entry" and genome["entry"]:
                genome["entry"][self.random.randrange(len(genome["entry"]))] = (
                    self.pick_atoms("entry", gran, 1, exclude=genome["entry"]) or genome["entry"][:1])[0]
            elif op == "swap_exit" and genome["exit"]:
                genome["exit"][self.random.randrange(len(genome["exit"]))] = (
                    self.pick_atoms("exit", gran, 1, exclude=genome["exit"]) or genome["exit"][:1])[0]
                genome["exit_mode"] = "own"
            elif op == "add_entry" and len(genome["entry"]) < most:
                genome["entry"] += self.pick_atoms("entry", gran, 1, exclude=genome["entry"])
            elif op == "drop_entry" and len(genome["entry"]) > 1:
                genome["entry"].pop(self.random.randrange(len(genome["entry"])))
            elif op == "add_exit" and len(genome["exit"]) < most:
                genome["exit"] += self.pick_atoms("exit", gran, 1, exclude=genome["exit"])
                genome["exit_mode"] = "own"
            elif op == "drop_exit" and len(genome["exit"]) > 2:
                genome["exit"].pop(self.random.randrange(len(genome["exit"])))
                genome["exit_mode"] = "own"
            elif op == "entry_k":
                genome["entry_k"] = max(1, min(len(genome["entry"]), genome["entry_k"] + self.random.choice([-1, 1])))
            elif op == "exit_k":
                genome["exit_k"] = max(0, min(len(genome["exit"]) - 1, genome["exit_k"] + self.random.choice([-1, 1])))
            elif op == "setting":
                setting = self.random.choice(GENOME_SETTINGS)
                genome[setting] = self.step(setting, genome.get(setting, self.options[setting][0]))
            elif op == "neighbor":
                side = self.random.choice(["entry", "exit"])
                if genome[side]:
                    index = self.random.randrange(len(genome[side]))
                    genome[side][index] = self.neighbor(genome[side][index], gran)
            elif op == "gran":
                genome["gran"] = self.choose("gran")
        genome["entry_k"] = max(1, min(genome["entry_k"], len(genome["entry"])))
        genome["exit_k"] = max(0, min(genome["exit_k"], len(genome["exit"]) - 1))
        return genome

    def step(self, setting, value):
        """Move a setting to a neighbouring option, or (sometimes) to a weighted random one."""
        options = self.options[setting]
        if self.random.random() < 0.3 or value not in options:
            return self.choose(setting)
        index = options.index(value) + self.random.choice([-1, 1])
        return options[max(0, min(len(options) - 1, index))]

    def neighbor(self, expr, gran):
        """An atom of the same family, preferring a nearby size."""
        family = self.by_family[self.atoms[expr]["family"]]
        candidates = [e for e in family if e != expr and self.atom_ok(e, gran)]
        if not candidates:
            return expr
        position = family.index(expr)
        candidates.sort(key=lambda e: abs(family.index(e) - position))
        return self.random.choice(candidates[:self.breeding["neighbor_span"]])

    def crossover(self, a, b):
        child = json.loads(json.dumps(self.random.choice([a, b])))
        pool = list(dict.fromkeys(a["entry"] + b["entry"]))
        size = self.random.choice([len(a["entry"]), len(b["entry"])])
        child["entry"] = self.random.sample(pool, min(size, len(pool)))
        donor = self.random.choice([a, b])
        child["exit"], child["exit_k"], child["exit_mode"] = list(donor["exit"]), donor["exit_k"], donor.get("exit_mode", "own")
        child["entry_k"] = max(1, round(len(child["entry"]) * a["entry_k"] / max(len(a["entry"]), 1)))
        for setting in GENOME_SETTINGS + ["gran"]:
            # A genome recorded before a setting existed doesn't carry it: that means the setting is off.
            child[setting] = self.random.choice([a, b]).get(setting) or child.get(setting) or 0
        return child

    def repair(self, genome):
        """Drop atoms that are invalid on the genome's candle size, then build and validate."""
        for setting in GENOME_SETTINGS:
            if genome.get(setting) is None:
                genome[setting] = 0  # recorded before the setting existed: off
        if genome.get("gran") not in self.granularities:
            genome["gran"] = self.choose("gran")
        gran = genome["gran"]
        for side in ("entry", "exit", "confirm"):
            genome[side] = list(dict.fromkeys(e for e in genome.get(side) or [] if e in self.atoms and self.atom_ok(e, gran)))
        if not genome["entry"] or len(genome["exit"]) < 1:
            return None
        if not genome["confirm"]:
            genome["confirm_k"] = 0
        genome["entry_k"] = max(1, min(genome["entry_k"], len(genome["entry"])))
        genome["exit_k"] = max(0, min(genome["exit_k"], len(genome["exit"]) - 1))
        profile = build_profile(genome, self.atoms, self.template, self.product_id)
        if self.validate(profile, GRANULARITY_SECONDS[gran]):
            return None
        return genome, profile

    def tournament(self, elites):
        contenders = self.random.sample(elites, min(self.breeding["tournament_size"], len(elites)))
        return max(contenders, key=lambda e: e["fitness"])["genome"]


def generator_worker(worker_id, atoms, template, settings, tasks, results):
    from performance_log import profile_fingerprint
    breeder = Breeder(atoms, template, settings, seed=worker_id * 7919 + int(time.time()))
    options = breeder.options
    while True:
        task = tasks.get()
        if task is None:
            return
        breeder.weights = task["weights"]
        breeder.random.seed(task["seed"])
        made, attempts = [], 0
        seeds = list(task.get("seeds", []))
        elites = task["elites"]
        while len(made) < task["count"] and attempts < task["count"] * 30:
            attempts += 1
            if seeds:
                origin, genome = seeds.pop(0)
            else:
                roll = breeder.random.random()
                if elites and roll < task["mutate"]:
                    origin, genome = "mutation", breeder.mutate(breeder.tournament(elites))
                elif len(elites) > 1 and roll < task["mutate"] + task["crossover"]:
                    origin, genome = "crossover", breeder.crossover(breeder.tournament(elites), breeder.tournament(elites))
                else:
                    origin, genome = "fresh", breeder.fresh()
            try:
                repaired = breeder.repair(genome)
            except Exception:  # a malformed candidate is skipped, never fatal
                repaired = None
            if repaired is None:
                continue
            genome, profile = repaired
            key = profile_fingerprint(profile)
            if key in task["seen"]:
                continue
            task["seen"].add(key)
            made.append({"key": key, "origin": origin, "genome": genome, "profile": profile,
                         "features": features(genome, atoms, options)})
        results.put((worker_id, task["generation"], made))


# --- evaluator worker ----------------------------------------------------------------

class _MemoryMetrics:
    """Shares indicator values between backtests in memory only, so nothing writes to candles.db."""

    def __init__(self):
        self._caches = {}

    def cache(self, candles):
        from indicators import MetricCache
        key = (len(candles), candles[0]["start"], candles[-1]["start"])
        if key not in self._caches:
            self._caches[key] = MetricCache(candles)
        return self._caches[key]


def summary_fitness(summary, fitness):
    """Annualized, drawdown-adjusted log growth; models that barely trade, or (with the optional
    `max_trades`) trade too often, are marked down by `trade_penalty` per trade outside the range."""
    years = max(summary.get("years", 1.0), 0.25)
    growth = max(1 + summary["return"], 1e-6)
    drawdown = min(summary["max_drawdown"], 0.99)
    score = (math.log(growth) + fitness["drawdown_weight"] * math.log(1 - drawdown)) / years
    score -= fitness["trade_penalty"] * max(0, fitness["min_trades"] - summary["trades"])
    if fitness.get("max_trades") is not None:
        score -= fitness["trade_penalty"] * max(0, summary["trades"] - fitness["max_trades"])
    return score


def fitness_of(result, fitness):
    return summary_fitness(_summary(result), fitness)


def _summary(result, fitness=None):
    """The measures a search keeps of one backtest; with `fitness` settings, its score too."""
    summary = {"return": result["total_return"], "buy_and_hold": result["buy_and_hold_return"],
               "max_drawdown": result["max_drawdown"], "trades": result["trade_count"],
               "win_rate": result["win_rate"],
               "years": (result["period_end"] - result["period_start"]) / (365.25 * 86400)}
    if fitness is not None:
        summary["fitness"] = summary_fitness(summary, fitness)
    return summary


def rescore(scores, fitness):
    """Recompute the fitness of stored summaries ({"train", "holdout"}, the training one possibly
    holding a "validation" summary) under the current fitness settings; returns `scores`."""
    for summary in (scores["train"], scores["holdout"], scores["train"].get("validation")):
        if summary is not None:
            summary["fitness"] = summary_fitness(summary, fitness)
    return scores


def selection_fitness(scores):
    """What the search selects on: the training fitness, or with a validation period the weaker
    of the training and validation fitness, so a model must work in both."""
    validation = scores["train"].get("validation")
    return scores["train"]["fitness"] if validation is None else min(scores["train"]["fitness"],
                                                                      validation["fitness"])


def conditions(genome):
    return len(genome["entry"]) + len(genome["exit"]) + len(genome.get("confirm") or [])


def rank_key(model):
    """Best first: higher fitness; among ties, the simpler model (fewer conditions), then fewer trades."""
    return (model["fitness"], -conditions(model["genome"]), -model["train"]["trades"])


def evaluator_worker(settings, friction_inputs, train_start, holdout_start, tasks, results,
                     compute_device="cpu", device_id=0):
    from backtest import DEFAULT_CACHE_DIR, load_cached_candles, run_backtest
    from candle_store import MarketCandles
    from friction import FrictionModel

    product_id, fitness = settings["market"]["product"], settings["fitness"]
    validation_start = settings["windows"].get("validation_start")
    validation_start = _timestamp(validation_start) if validation_start else None
    markets = {}
    for gran in settings["market"]["granularities"]:
        # Read-only: refresh_market, before the workers start, is the only writer.
        loaded = load_cached_candles(DEFAULT_CACHE_DIR, product_id, gran, read_only=True)
        if loaded is None:
            continue
        candles = list(loaded[0])
        markets[gran] = {
            "full": MarketCandles(candles, _MemoryMetrics()),
            "train": MarketCandles([c for c in candles if c["start"] < holdout_start], _MemoryMetrics()),
        }
    while True:
        task = tasks.get()
        if task is None:
            return
        out = []
        for item in task["batch"]:
            profile, gran = item["profile"], item["profile"]["period_granularity"]
            friction = FrictionModel(profile, friction_inputs)
            try:
                if task["mode"] == "score":
                    train = run_backtest(
                        markets[gran]["train"], profile, friction=friction, start_time=train_start,
                        compute_device=compute_device, device_id=device_id)
                    holdout = run_backtest(
                        markets[gran]["full"], profile, friction=friction, start_time=holdout_start,
                        compute_device=compute_device, device_id=device_id)
                    scores = {"key": item["key"], "train": _summary(train, fitness),
                              "holdout": _summary(holdout, fitness)}
                    if validation_start is not None:
                        # The last part of the training window, scored on its own.
                        validation = run_backtest(markets[gran]["train"], profile, friction=friction,
                                                  start_time=validation_start,
                                                  compute_device=compute_device, device_id=device_id)
                        scores["train"]["validation"] = _summary(validation, fitness)
                    out.append(scores)
                else:  # "full": the simulator's own run, for the performance log
                    result = run_backtest(
                        markets[gran]["full"], profile, friction=friction,
                        compute_device=compute_device, device_id=device_id)
                    result["model_name"], result["granularity"] = item["name"], gran
                    for heavy in ("series", "indicators", "rules", "trades", "friction"):
                        result.pop(heavy, None)
                    out.append({"key": item["key"], "name": item["name"], "result": result})
            except Exception as exc:  # a broken candidate must not stop the search
                out.append({"key": item["key"], "error": f"{type(exc).__name__}: {exc}"})
        results.put((task["batch_id"], out))


# --- analyst worker ------------------------------------------------------------------

def analyst_worker(reference_records, analysis, tasks, results):
    """Turns scored models into generator weights and a characteristics report."""

    def weights_from(lift_table):
        return {feat: max(analysis["weight_floor"], min(analysis["weight_cap"], lift))
                for feat, (lift, _, _) in lift_table.items()}

    def table(lift_table, minimum):
        rows = [(f, round(l, 3), n, t) for f, (l, n, t) in lift_table.items() if n >= minimum and "_atom=" not in f]
        rows.sort(key=lambda r: r[1], reverse=True)
        return {"best": rows[:25], "worst": rows[-10:][::-1]}

    def atom_table(lift_table, minimum):
        rows = [(f, round(l, 3), n, t) for f, (l, n, t) in lift_table.items() if n >= minimum and "_atom=" in f]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:20]

    reference = lifts(reference_records, analysis["top_fraction"], analysis["shrink"])
    reference_weights = weights_from(reference)
    records = []
    while True:
        task = tasks.get()
        if task is None:
            return
        records.extend(task["records"])
        current = lifts(records, analysis["top_fraction"], analysis["shrink"])
        current_weights = weights_from(current)
        # The reference models lead at first; the market's own evidence takes over as it builds up.
        trust = 1 / (1 + len(records) / analysis["reference_half_weight_models"])
        weights = {}
        for feat in set(reference_weights) | set(current_weights):
            weights[feat] = reference_weights.get(feat, 1.0) ** trust * current_weights.get(feat, 1.0) ** (1 - trust)
        report = {"generation": task["generation"], "models_scored": len(records), "reference_trust": round(trust, 3),
                  "market": table(current, analysis["report_min_support"]),
                  "market_atoms": atom_table(current, analysis["report_min_atom_support"]),
                  "reference": table(reference, analysis["reference_min_support"]) if task["generation"] == 0 else None}
        results.put({"weights": weights, "report": report})


# --- coordinator ---------------------------------------------------------------------

def _timestamp(text):
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def refresh_market(product_id, granularities, offline):
    """Download new candles and the live fee tier through the simulator's own client."""
    from backtest import load_candles, lookback_window, resolve_granularity
    inputs = {"exchange_fees": None, "exchange_fee_error": "offline", "network_fee": None, "network_fee_error": None}
    if offline:
        return inputs
    from engine import QuantitativeTradingEngine
    from friction import fetch_friction_inputs
    client = QuantitativeTradingEngine(base_directory=".", node_is_primary=False).create_coinbase_client()
    for gran in granularities:
        _, seconds = resolve_granularity(gran)
        candles = load_candles(client, product_id, gran, *lookback_window(seconds))
        print(f"  {product_id} {gran}: {len(candles):,} candles", flush=True)
    template = {"product_id": product_id, "ticker": product_id.split("-")[0]}
    return fetch_friction_inputs(client, [template])


# --- variety: unique behaviour and diverse output ------------------------------------

def signature(model):
    """What a scored model did: two models with the same signature traded identically."""
    return (model["genome"]["gran"],) + tuple(
        (model[window]["trades"], round(model[window]["return"], 6), round(model[window]["max_drawdown"], 6))
        for window in ("train", "holdout"))


def unique_by_behaviour(models):
    """The models (best first) without any that only repeat a better model's trades."""
    seen, out = set(), []
    for model in models:
        key = signature(model)
        if key not in seen:
            seen.add(key)
            out.append(model)
    return out


def similarity(a, b):
    """How alike two genomes are: the share of conditions they have in common, 0 if the candles differ."""
    if a["gran"] != b["gran"]:
        return 0.0
    sides = [set(genome["entry"]) | set(genome["exit"]) | set(genome.get("confirm") or []) for genome in (a, b)]
    return len(sides[0] & sides[1]) / max(len(sides[0] | sides[1]), 1)


def select_diverse(ranked, count, max_similarity):
    """Up to `count` models, best first, skipping repeats of a better model's trades and any model whose
    conditions overlap a chosen one's by more than `max_similarity` (1 keeps every distinct model)."""
    chosen = []
    for model in unique_by_behaviour(ranked):
        if len(chosen) >= count:
            break
        if all(similarity(model["genome"], other["genome"]) <= max_similarity for other in chosen):
            chosen.append(model)
    return chosen


def outperforming_buy_and_hold(models):
    """Keep models whose training-window ending value strictly beats buy and hold."""
    return [model for model in models
            if model["train"]["return"] > model["train"]["buy_and_hold"]]


# --- naming and writing models -------------------------------------------------------

def _library_files(config_dir):
    """Every profile and discarded profile in the library `config_dir` belongs to."""
    from model_library import discard_folders, library_root, profile_paths
    root = library_root(config_dir)
    return profile_paths(root) + [path for folder in discard_folders(root) for path in sorted(folder.glob("*.json"))]


def used_names(config_dir, performance_path):
    """Every model name in use anywhere in the library (any currency folder or discard folder) or in the
    performance log."""
    names = {path.stem for path in _library_files(config_dir)}
    if Path(performance_path).exists():
        with Path(performance_path).open(newline="", encoding="utf-8") as handle:
            names |= {row["model"] for row in csv.DictReader(handle)}
    return names


def existing_fingerprints(config_dir):
    """{fingerprint: name} of the profiles already anywhere in the library, discard folders included."""
    from performance_log import profile_fingerprint
    found = {}
    for path in _library_files(config_dir):
        try:
            found.setdefault(profile_fingerprint(json.loads(path.read_text(encoding="utf-8"))), path.stem)
        except (OSError, ValueError):
            continue
    return found


def write_models(models, prefix, config_dir, performance_path):
    """Write each model as config/<prefix>_###.json under the next free numbers, setting model["name"].

    Holds the models lock, so searches running at the same time never take the same name. A
    name is never reused, even one only in the discard folder or the performance log, and a
    profile identical to one already in config/ or the discard folder is skipped, and that
    model's name is set as model["existing"]. Returns the models written.
    """
    from model_library import models_lock
    from performance_log import profile_fingerprint
    pattern = re.compile(re.escape(prefix) + r"_(\d+)$")
    written = []
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    with models_lock(config_dir):
        taken = used_names(config_dir, performance_path)
        number = max((int(match[1]) for name in taken if (match := pattern.match(name))), default=0)
        known = existing_fingerprints(config_dir)
        for model in models:
            existing = known.get(profile_fingerprint(model["profile"]))
            if existing is not None:
                model["existing"] = existing
                continue
            number += 1
            model["name"] = f"{prefix}_{number:03d}"
            with (Path(config_dir) / f"{model['name']}.json").open("w", encoding="utf-8") as handle:
                json.dump(model["profile"], handle, indent=2)
                handle.write("\n")
            written.append(model)
    return written


# --- settings and command line -------------------------------------------------------

# Optional sections, with their defaults; a settings file from an earlier version needs none of them.
OPTIONAL_SETTINGS = {
    "goals": None,  # goals file for the ranking (see model_assessor.py); None: search_config/goals.json if present
    "references": None,  # profiles to learn from: paths or globs; None: every profile in config/
    "models_dir": None,  # folder the models are written to and ranked in; None: config/
    "reference_scores": True,  # weight seeds and the analyst by the references' logged (full-history) results
    "variety": {"max_similarity": 0.8, "min_fitness": None, "require_train_outperformance": False},
    "registry": {"enabled": True, "migrants": 10},
    "ranking": {"enabled": True},
}


def with_optional_settings(settings):
    for section, default in OPTIONAL_SETTINGS.items():
        if isinstance(default, dict):
            settings[section] = {**default, **(settings.get(section) or {})}
        else:
            settings.setdefault(section, default)
    return settings


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS),
                        help="search settings file (default: search_config/model_search.json)")
    parser.add_argument("--product", help="override market.product")
    parser.add_argument("--prefix", help="override market.prefix")
    for name in ("population", "keep", "batch-size", "min-generations", "max-generations", "seed"):
        parser.add_argument(f"--{name}", type=int, help=f"override search.{name.replace('-', '_')}")
    for name in ("generators", "evaluators"):
        parser.add_argument(f"--{name}", help=f"override search.{name}: a number, or auto")
    parser.add_argument("--instances", type=int, default=1,
                        help="run this many searches at once, sharing the CPUs, the registry and the ranking")
    parser.add_argument("--goals", help="goals file for the ranking (default: settings 'goals', else "
                                        "search_config/goals.json if present)")
    parser.add_argument("--references", nargs="+", metavar="PATTERN",
                        help="learn from these profiles only (paths or globs, such as 'config/doge_*.json') "
                             "instead of every profile in config/")
    parser.add_argument("--models-dir", help="write and rank models in this folder instead of config/")
    parser.add_argument("--no-reference-scores", action="store_true",
                        help="use the references' structures only, not their logged results (which cover the "
                             "full history, holdout included)")
    parser.add_argument("--no-rank", action="store_true",
                        help="skip ranking the currency's models and moving the bottom to config/discard/")
    parser.add_argument("--offline", action="store_true", help="use cached candles and the profile's fallback fee")
    parser.add_argument("--compute-device", choices=("auto", "cpu", "cuda"), default="auto",
                        help="auto (default) uses visible CUDA GPUs; cpu or cuda forces the device choice")
    parser.add_argument("--dry-run", action="store_true",
                        help="keep the profiles in the run folder; leave config/ and the performance log alone")
    parser.add_argument("--friction-file", help=argparse.SUPPRESS)  # set by --instances for its searches
    parser.add_argument("--share", help=argparse.SUPPRESS)  # "i/n": this search is the i-th of n (from 0)
    parser.add_argument("--gpu-worker-slots", type=int, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def apply_overrides(settings, args):
    for key in ("product", "prefix"):
        if getattr(args, key) is not None:
            settings["market"][key] = getattr(args, key)
    for key in ("population", "keep", "generators", "evaluators", "batch_size", "min_generations",
                "max_generations", "seed"):
        if getattr(args, key) is not None:
            settings["search"][key] = getattr(args, key)
    settings["search"]["compute_device"] = args.compute_device
    if args.goals:
        settings["goals"] = args.goals
    if args.references:
        settings["references"] = args.references
    if args.models_dir:
        settings["models_dir"] = args.models_dir
    if args.no_reference_scores:
        settings["reference_scores"] = False
    return settings


def worker_count(value, instances=1, gpu_slots=0):
    """CPU worker count plus this search's share of extra GPU-backed evaluator slots."""
    from parallel_backtest import max_workers
    if str(value).strip().lower() == "auto":
        return max(1, (max_workers() + gpu_slots) // max(instances, 1))
    return max(1, int(value)) + math.ceil(gpu_slots / max(instances, 1))


def ranking_goals(settings):
    """The goals the ranking uses: the settings' goals file, else search_config/goals.json, else none
    (models are then ranked by score alone, with the default ranking settings)."""
    from model_assessor import DEFAULT_GOALS, check_goals, load_goals
    if settings.get("goals"):
        return load_goals(ROOT / settings["goals"]), settings["goals"]
    if DEFAULT_GOALS.exists():
        return load_goals(DEFAULT_GOALS), str(DEFAULT_GOALS.relative_to(ROOT))
    return check_goals({}), "no goals file (ranked by score)"


def rank_market(settings, friction_inputs, say):
    """Rank every model of the search's currency, wherever it sits in the library, and move the bottom to
    its currency's discard folder (model_assessor.py)."""
    from model_assessor import format_table, rank_and_discard
    from model_library import library_root
    from performance_log import DEFAULT_PATH as PERFORMANCE_LOG
    goals, source = ranking_goals(settings)
    product = settings["market"]["product"]
    say(f"Ranking {product} models against {source}")
    ranked, discarded = rank_and_discard(product, goals, friction_inputs, client=None,
                                         workers=worker_count("auto"),
                                         config_dir=library_root(models_dir(settings)),
                                         performance_path=PERFORMANCE_LOG, say=say)
    if ranked:
        top = format_table(ranked[:10]).splitlines()
        say("\n".join(["  " + line for line in top]))
    return ranked, discarded, source


def run_instances(args, argv, settings):
    """Run `args.instances` searches at once, then rank once. Each gets its share of the CPUs and its
    own seed; all share the market data and fees refreshed here, the registry and config/."""
    import subprocess
    import threading
    count, market, search = args.instances, settings["market"], settings["search"]
    print(f"Model search for {market['product']}: {count} searches at once; refreshing market data", flush=True)
    friction_inputs = refresh_market(market["product"], market["granularities"], args.offline)
    folder = SEARCH_DIR / f"{market['prefix']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_x{count}"
    folder.mkdir(parents=True)
    friction_file = folder / "friction.json"
    friction_file.write_text(json.dumps(friction_inputs), encoding="utf-8")
    base_seed = search["seed"] if search["seed"] is not None else random.SystemRandom().randrange(1 << 30)
    from parallel_backtest import resolve_compute_device
    compute_device, gpu_devices = resolve_compute_device(args.compute_device)
    workers = {"generators": worker_count(search["generators"], count),
               "evaluators": worker_count(search["evaluators"], count, len(gpu_devices))}
    gpu_slot_counts = [len(gpu_devices) // count + (index < len(gpu_devices) % count)
                       for index in range(count)]
    print(f"  each search: {workers['generators']} generators, {workers['evaluators']} evaluators; seeds "
          f"{base_seed} to {base_seed + count - 1}", flush=True)
    forwarded, skip = [], False
    for item in argv:  # everything but --instances, which the searches must not repeat
        if skip:
            skip = False
        elif item == "--instances":
            skip = True
        elif not item.startswith("--instances="):
            forwarded.append(item)
    children = []
    for index in range(count):
        command = [sys.executable, str(Path(__file__).resolve()), *forwarded, "--friction-file", str(friction_file),
                   "--no-rank", "--seed", str(base_seed + index), "--share", f"{index}/{count}",
                   "--generators", str(workers["generators"]),
                   "--evaluators", str(worker_count(search["evaluators"], count)),
                   "--gpu-worker-slots", str(gpu_slot_counts[index])]
        children.append(subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", bufsize=1))

    def relay(number, child):
        for line in child.stdout:
            print(f"[{number}] {line}", end="", flush=True)

    relays = [threading.Thread(target=relay, args=(number, child), daemon=True)
              for number, child in enumerate(children, start=1)]
    for thread in relays:
        thread.start()
    codes = [child.wait() for child in children]
    for thread in relays:
        thread.join()
    failed = [number for number, code in enumerate(codes, start=1) if code]
    print(f"{count - len(failed)} of {count} searches finished" + (f"; failed: {failed}" if failed else ""), flush=True)
    if not args.dry_run and not args.no_rank and settings["ranking"]["enabled"]:
        rank_market(settings, friction_inputs, lambda message: print(message, flush=True))
    if failed:
        sys.exit(1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    try:
        settings = with_optional_settings(apply_overrides(load_settings(args.settings), args))
    except SettingsError as exc:
        sys.exit(str(exc))
    if args.instances > 1:
        return run_instances(args, argv, settings)
    market, search = settings["market"], settings["search"]
    product, prefix = market["product"], market["prefix"]
    from parallel_backtest import resolve_compute_device
    compute_device, gpu_devices = resolve_compute_device(search["compute_device"])
    search["compute_device"] = compute_device
    search["generators"] = worker_count(search["generators"])
    gpu_worker_slots = len(gpu_devices) if args.gpu_worker_slots is None else args.gpu_worker_slots
    if gpu_worker_slots < 0 or gpu_worker_slots > len(gpu_devices):
        raise ValueError(f"GPU worker slots must be between 0 and {len(gpu_devices)}")
    search["evaluators"] = worker_count(search["evaluators"], gpu_slots=gpu_worker_slots)
    if search["seed"] is None:
        search["seed"] = random.SystemRandom().randrange(1 << 30)

    from performance_log import DEFAULT_PATH as PERFORMANCE_LOG, describe_update, update_performance_log

    # The process ID keeps two searches started in the same second apart.
    run_dir = SEARCH_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    run_dir.mkdir(parents=True)
    # The settings travel with the run, so its report can always be reproduced.
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    log_file = (run_dir / "search.log").open("w", encoding="utf-8")

    def say(message):
        print(message, flush=True)
        log_file.write(message + "\n")
        log_file.flush()

    if args.friction_file:
        say(f"Model search for {product} with {args.settings} (seed {search['seed']}): market data refreshed "
            "by the launcher")
        friction_inputs = json.loads(Path(args.friction_file).read_text(encoding="utf-8"))
    else:
        say(f"Model search for {product} with {args.settings} (seed {search['seed']}): refreshing market data")
        friction_inputs = refresh_market(product, market["granularities"], args.offline)
    say(f"  fees: {friction_inputs.get('exchange_fees')}")

    with profile_path(market["template_profile"]).open(encoding="utf-8") as handle:
        template = json.load(handle)
    library = expand_atoms(settings["atoms"])
    harvest, scored_reference = load_reference(library, PERFORMANCE_LOG, settings)
    atoms = harvest.atoms
    options = {"gran": market["granularities"], **settings["options"]}
    say(f"Reference: {len(harvest.genomes)} of {len(harvest.genomes) + len(harvest.skipped)} profiles "
        f"recovered as genomes, {len(scored_reference)} with logged results; "
        f"{len(atoms)} atoms ({len(atoms) - len(library)} harvested from the references)")
    if not settings["reference_scores"]:
        # Use the references' structures only: their logged full-history results would leak the holdout.
        say("Reference scores off: references seed the search unranked and don't weight the analyst")
        scored_reference = [(name, genome, 0.0) for name, genome in harvest.genomes]
        reference_records = []
    else:
        reference_records = [(features(genome, atoms, options), score) for _, genome, score in scored_reference]

    train_start, holdout_start = _timestamp(settings["windows"]["train_start"]), _timestamp(settings["windows"]["holdout_start"])
    registry = None
    if settings["registry"]["enabled"]:
        from backtest import DEFAULT_CACHE_DIR
        from candle_store import CandleStore
        from search_registry import SearchRegistry, context_key
        store = CandleStore(DEFAULT_CACHE_DIR, read_only=True)
        from backtest import BACKTEST_VERSION
        # Fitness settings aren't part of it: scores are recomputed from the stored summaries.
        context = context_key(product=product, windows=settings["windows"], backtest=BACKTEST_VERSION,
                              template={k: v for k, v in template.items() if k not in ("strategy", "indicators")},
                              fees=friction_inputs,
                              candles={gran: store.fetched_range(product, gran) for gran in market["granularities"]})
        registry = SearchRegistry(SEARCH_DIR / "registry.db", context, run_dir.name)
        say(f"Registry: {registry.count()} models already scored under these conditions (context {context})")

    ctx = mp.get_context("spawn")
    gen_tasks, gen_results = ctx.Queue(), ctx.Queue()
    eval_tasks, eval_results = ctx.Queue(), ctx.Queue()
    analyst_tasks, analyst_results = ctx.Queue(), ctx.Queue()
    workers = [ctx.Process(target=generator_worker, args=(i, atoms, template, settings, gen_tasks, gen_results),
                           name=f"generator-{i + 1}") for i in range(search["generators"])]
    workers += [
        ctx.Process(
            target=evaluator_worker,
            args=(settings, friction_inputs, train_start, holdout_start, eval_tasks, eval_results,
                  search["compute_device"],
                  gpu_devices[i % len(gpu_devices)] if gpu_devices else 0),
            name=f"evaluator-{i + 1}")
        for i in range(search["evaluators"])
    ]
    workers.append(ctx.Process(target=analyst_worker,
                               args=(reference_records, settings["analysis"], analyst_tasks, analyst_results),
                               name="analyst"))
    for worker in workers:
        worker.start()
    gpu_slots = gpu_worker_slots
    say(f"Workers: {search['generators']} generators, {search['evaluators']} evaluator(s)"
        + (f" including {gpu_slots} GPU-added slot(s)" if gpu_slots else "")
        + f" (batches of {search['batch_size']}), 1 analyst; compute device {search['compute_device']}"
        + (f" across CUDA devices {gpu_devices}" if gpu_devices else ""))

    def receive(channel):
        """The next message from a worker queue; fails loudly if a worker has died."""
        while True:
            try:
                return channel.get(timeout=5)
            except queue.Empty:
                dead = [worker.name for worker in workers if not worker.is_alive()]
                if dead:
                    raise RuntimeError(f"Worker(s) stopped unexpectedly: {', '.join(dead)}; see the traceback above")

    rng = random.Random(search["seed"])
    models = {}  # key -> record
    seen = set()
    history = []
    batch_counter = 0
    stats = {"reused": 0, "migrants": 0}

    # Generation 0 starts from the reference characteristics alone.
    analyst_tasks.put({"generation": 0, "records": []})
    weights = receive(analyst_results)["weights"]
    reference_seeds = sorted(scored_reference, key=lambda r: r[2], reverse=True)
    seeds = [("reference:" + name, json.loads(json.dumps(genome))) for name, genome, _ in reference_seeds]
    if args.share:
        # Searches launched together start from different reference models: each leads with its own share.
        index, count = (int(part) for part in args.share.split("/"))
        seeds = seeds[index::count] + [seed for position, seed in enumerate(seeds) if position % count != index]
    generators = search["generators"]

    def breed(generation, count, seeds=()):
        # Parents come from the best models that each behave differently.
        elites = unique_by_behaviour(sorted((m for m in models.values() if "train" in m),
                                            key=rank_key, reverse=True))
        elite_payload = [{"genome": m["genome"], "fitness": m["fitness"]} for m in elites[:search["elites"]]]
        made = []
        for round_ in range(6):
            need = count - len(made)
            if need <= 0:
                break
            per_worker = math.ceil(need / generators)
            seed_chunks = [list(seeds[i::generators]) if round_ == 0 else [] for i in range(generators)]
            for i in range(generators):
                gen_tasks.put({"generation": generation, "count": per_worker, "seed": rng.randrange(1 << 30),
                               "elites": elite_payload, "weights": weights, "seen": set(seen),
                               "seeds": seed_chunks[i],
                               "mutate": 0.0 if generation == 0 else settings["breeding"]["mutate"],
                               "crossover": 0.0 if generation == 0 else settings["breeding"]["crossover"]})
            for _ in range(generators):
                _, _, batch = receive(gen_results)
                for candidate in batch:
                    if candidate["key"] not in seen and len(made) < count:
                        seen.add(candidate["key"])
                        candidate["generation"] = generation
                        made.append(candidate)
        return made

    def evaluate(candidates, mode="score"):
        nonlocal batch_counter
        out = []
        if mode == "score" and registry is not None:
            # Models any search already scored under these conditions aren't backtested again.
            known = registry.lookup(c["key"] for c in candidates)
            out = [{"key": key, **rescore(scores, settings["fitness"])} for key, scores in known.items()]
            stats["reused"] += len(known)
            candidates = [c for c in candidates if c["key"] not in known]
        pending = {}
        for start in range(0, len(candidates), search["batch_size"]):
            batch = candidates[start:start + search["batch_size"]]
            batch_counter += 1
            pending[batch_counter] = len(batch)
            eval_tasks.put({"batch_id": batch_counter, "mode": mode,
                            "batch": [{"key": c["key"], "profile": c["profile"], "name": c.get("name")} for c in batch]})
        fresh = []
        while pending:
            batch_id, results = receive(eval_results)
            pending.pop(batch_id)
            fresh.extend(results)
        if mode == "score" and registry is not None:
            genomes = {c["key"]: c["genome"] for c in candidates}
            registry.record([{"key": r["key"], "genome": genomes[r["key"]], "train": r["train"],
                              "holdout": r["holdout"], "fitness": selection_fitness(r)}
                             for r in fresh if "train" in r])
        return out + fresh

    def welcome_migrants(generation):
        """Add the best models other searches have found, so they can be parents here too."""
        if registry is None or not settings["registry"]["migrants"]:
            return 0
        from performance_log import profile_fingerprint
        joined = 0
        for record in registry.best(settings["registry"]["migrants"],
                                    key=lambda r: selection_fitness(rescore(r, settings["fitness"]))):
            genome = record["genome"]
            if record["key"] in models or genome.get("gran") not in market["granularities"]:
                continue
            if not all(expr in atoms for side in ("entry", "exit", "confirm") for expr in genome.get(side) or []):
                continue  # built from conditions this search doesn't have
            profile = build_profile(genome, atoms, template, product)
            if profile_fingerprint(profile) != record["key"]:
                continue  # built differently here (another template), so its scores don't apply
            seen.add(record["key"])
            models[record["key"]] = {
                "key": record["key"], "origin": "migrant:" + record["instance"], "genome": genome,
                "profile": profile, "features": features(genome, atoms, options), "generation": generation,
                "train": record["train"], "holdout": record["holdout"], "fitness": selection_fitness(record)}
            joined += 1
        stats["migrants"] += joined
        return joined

    written, ranked, full = [], [], {}
    try:
        generation = 0
        while True:
            started = time.time()
            candidates = breed(generation, search["population"], seeds if generation == 0 else ())
            reused_before = stats["reused"]
            results = {r["key"]: r for r in evaluate(candidates)}
            new_records, errors = [], 0
            for candidate in candidates:
                result = results.get(candidate["key"], {})
                if "error" in result or "train" not in result:
                    errors += 1
                    continue
                candidate.update(train=result["train"], holdout=result["holdout"], fitness=selection_fitness(result))
                models[candidate["key"]] = candidate
                new_records.append((candidate["features"], candidate["fitness"]))
            migrants = welcome_migrants(generation)
            analyst_tasks.put({"generation": generation, "records": new_records})
            analysis = receive(analyst_results)
            weights = analysis["weights"]
            with (run_dir / f"characteristics_gen{generation:02d}.json").open("w", encoding="utf-8") as handle:
                json.dump(analysis["report"], handle, indent=2)

            ranked = sorted(models.values(), key=rank_key, reverse=True)
            # Clones that trade identically would make the top 20 look settled; count each behaviour once.
            ranked = unique_by_behaviour(ranked)
            best, top20 = ranked[0], ranked[:20]
            top20_mean = sum(m["fitness"] for m in top20) / len(top20)
            gen_best = max((c["fitness"] for c in candidates if "fitness" in c), default=float("nan"))
            origins = Counter(c["origin"].split(":")[0] for c in sorted(
                (c for c in candidates if "fitness" in c), key=lambda c: c["fitness"], reverse=True)[:20])
            history.append({"generation": generation, "candidates": len(candidates), "errors": errors,
                            "reused": stats["reused"] - reused_before, "migrants": migrants,
                            "generation_best": gen_best, "best_fitness": best["fitness"], "top20_mean": top20_mean,
                            "best_train_return": best["train"]["return"], "best_holdout_return": best["holdout"]["return"],
                            "top20_holdout_mean_return": sum(m["holdout"]["return"] for m in top20) / len(top20),
                            "seconds": round(time.time() - started, 1)})
            say(f"Gen {generation:2d}: {len(candidates)} bred, {errors} failed, "
                f"{stats['reused'] - reused_before} already scored, {migrants} migrants | gen best {gen_best:+.3f} | "
                f"all-time best {best['fitness']:+.3f} (train {best['train']['return']:+.0%}, "
                f"holdout {best['holdout']['return']:+.0%}, dd {best['train']['max_drawdown']:.0%}) | "
                f"top-20 mean {top20_mean:+.3f} | top-20 of gen from {dict(origins)} | {time.time() - started:.0f}s")
            top_feats = [f"{f} x{l}" for f, l, n, t in analysis["report"]["market"]["best"][:6]]
            say(f"        analyst: {', '.join(top_feats)}")

            plateau = False
            if generation + 1 >= search["min_generations"] and len(history) > search["patience"]:
                then = history[-1 - search["patience"]]
                plateau = (best["fitness"] - then["best_fitness"] < search["tolerance"]
                           and top20_mean - then["top20_mean"] < search["tolerance"])
            if plateau or generation + 1 >= search["max_generations"]:
                say(f"Stopping after generation {generation}: "
                    + ("improvement plateaued" if plateau else "generation limit reached"))
                break
            generation += 1

        # --- write the best models, each behaving differently --------------------------
        unique = unique_by_behaviour(sorted(models.values(), key=rank_key, reverse=True))
        floor = settings["variety"].get("min_fitness")
        eligible = [m for m in unique if floor is None or m["fitness"] >= floor]
        if settings["variety"]["require_train_outperformance"]:
            eligible = outperforming_buy_and_hold(eligible)
        ranked = select_diverse(eligible, search["keep"], settings["variety"]["max_similarity"])
        say(f"Variety: {len(models)} models scored, {len(unique)} with distinct trades; kept {len(ranked)} "
            f"no more than {settings['variety']['max_similarity']:.0%} alike")
        if settings["variety"]["require_train_outperformance"]:
            say(f"Buy-and-hold gate: {len(eligible)} distinct models beat buy and hold in the training window")
        if args.dry_run:
            profile_dir = run_dir / "profiles"
            profile_dir.mkdir(exist_ok=True)
            for number, model in enumerate(ranked, start=1):
                model["name"] = f"{prefix}_{number:03d}"
                (profile_dir / f"{model['name']}.json").write_text(json.dumps(model["profile"], indent=2) + "\n",
                                                                   encoding="utf-8")
            written = ranked
        else:
            profile_dir = models_dir(settings)
            profile_dir.mkdir(parents=True, exist_ok=True)
            written = write_models(ranked, prefix, profile_dir, PERFORMANCE_LOG)
        skipped = [m["existing"] for m in ranked if m.get("existing")]
        say(f"Wrote {len(written)} profiles to {profile_dir}"
            + (f"; {len(skipped)} already there, from an earlier or a parallel search: {', '.join(skipped)}"
               if skipped else ""))

        full = {r["key"]: r for r in evaluate(written, mode="full")} if written else {}
        entries = [(full[m["key"]]["result"], m["profile"]) for m in written if "result" in full.get(m["key"], {})]
        if args.dry_run:
            say("Dry run: performance log left untouched")
        elif entries:
            try:
                say(describe_update(update_performance_log(entries, PERFORMANCE_LOG), PERFORMANCE_LOG))
            except OSError as exc:
                say(f"Performance log not updated ({exc})")
    finally:
        for _ in range(search["generators"]):
            gen_tasks.put(None)
        for _ in range(search["evaluators"]):
            eval_tasks.put(None)
        analyst_tasks.put(None)
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()

    # --- rank the currency's models; the bottom move to config/discard/ ----------------
    ranking = None
    try:
        if not args.dry_run and not args.no_rank and settings["ranking"]["enabled"]:
            ranking = rank_market(settings, friction_inputs, say)
        if written:
            write_outputs(run_dir, settings, args, models, written, full, history, harvest, friction_inputs,
                          stats, ranking)
            say(f"Report: {run_dir / 'report.md'}")
    finally:
        log_file.close()


def write_outputs(run_dir, settings, args, models, ranked, full, history, harvest, friction_inputs, stats=None,
                  ranking=None):
    market, windows, fitness = settings["market"], settings["windows"], settings["fitness"]
    with (run_dir / "generations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(history[0]))
        writer.writeheader()
        writer.writerows(history)

    columns = ["name", "key", "generation", "origin", "gran", "fitness", "train_return", "train_max_drawdown",
               "train_trades", "holdout_return", "holdout_max_drawdown", "holdout_trades", "holdout_fitness"]
    names = {m["key"]: m["name"] for m in ranked}
    with (run_dir / "all_models.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for m in sorted(models.values(), key=lambda m: m["fitness"], reverse=True):
            writer.writerow({"name": names.get(m["key"], ""), "key": m["key"], "generation": m["generation"],
                             "origin": m["origin"], "gran": m["genome"]["gran"], "fitness": round(m["fitness"], 4),
                             "train_return": round(m["train"]["return"], 4),
                             "train_max_drawdown": round(m["train"]["max_drawdown"], 4),
                             "train_trades": m["train"]["trades"], "holdout_return": round(m["holdout"]["return"], 4),
                             "holdout_max_drawdown": round(m["holdout"]["max_drawdown"], 4),
                             "holdout_trades": m["holdout"]["trades"],
                             "holdout_fitness": round(m["holdout"]["fitness"], 4)})
    with (run_dir / "genomes.json").open("w", encoding="utf-8") as handle:
        json.dump({m["name"]: m["genome"] for m in ranked}, handle, indent=2)

    final = json.loads(sorted(run_dir.glob("characteristics_gen*.json"))[-1].read_text(encoding="utf-8"))
    first = json.loads((run_dir / "characteristics_gen00.json").read_text(encoding="utf-8"))
    bh_train = ranked[0]["train"]["buy_and_hold"]
    bh_holdout = ranked[0]["holdout"]["buy_and_hold"]
    destination = ("the run folder (dry run)" if args.dry_run else
                   f"`config/` as {ranked[0]['name']} to {ranked[-1]['name']}")
    stats = stats or {"reused": 0, "migrants": 0}
    lines = [
        f"# Model search: {market['product']}",
        "",
        f"Run {run_dir.name} with `{args.settings}` (copied to `settings.json`). {len(models)} unique models "
        f"scored over {len(history)} generations; the top {len(ranked)}, each trading differently and no more "
        f"than {settings['variety']['max_similarity']:.0%} alike, were written to {destination}.",
        "",
        f"- Shared registry: {stats['reused']} candidates were already scored by this or another search and "
        f"not backtested again; {stats['migrants']} of other searches' best models joined as parents.",
        f"- Training window (selection): {windows['train_start']} to {windows['holdout_start']}; the top "
        f"model's buy and hold (from its first possible buy) {bh_train:+.1%}.",
        f"- Holdout window (never used for selection): {windows['holdout_start']} to the latest candle; the top "
        f"model's buy and hold {bh_holdout:+.1%}.",
        f"- Fees: {friction_inputs.get('exchange_fees')}; slippage and tax as in `{market['template_profile']}`.",
        f"- Compute device: `{settings['search'].get('compute_device', 'cpu')}` for supported moving averages; "
        "rule evaluation and the stateful trading simulation use the CPU.",
        f"- Fitness: annualized log growth plus {fitness['drawdown_weight']} x log(1 - max drawdown), less "
        f"{fitness['trade_penalty']} per trade under {fitness['min_trades']}.",
        "",
        "## Generations",
        "",
        "| Gen | Bred | Gen best | All-time best | Top-20 mean | Best train | Best holdout | Top-20 holdout mean |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in history:
        lines.append(f"| {h['generation']} | {h['candidates']} | {h['generation_best']:+.3f} | {h['best_fitness']:+.3f} | "
                     f"{h['top20_mean']:+.3f} | {h['best_train_return']:+.0%} | {h['best_holdout_return']:+.0%} | "
                     f"{h['top20_holdout_mean_return']:+.0%} |")
    lines += ["", "## Top 20 models", "",
              "| Model | Candles | Fitness | Train return | Train DD | Holdout return | Holdout DD | "
              "Full-history return | vs buy & hold | Trades |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for m in ranked[:20]:
        result = full.get(m["key"], {}).get("result")
        full_ret = f"{result['total_return']:+.0%}" if result else "n/a"
        vs = (f"{(1 + result['total_return']) / (1 + result['buy_and_hold_return']) - 1:+.0%}"
              if result and result["buy_and_hold_return"] > -1 else "n/a")
        lines.append(f"| {m['name']} | {m['genome']['gran']} | {m['fitness']:+.3f} | {m['train']['return']:+.0%} | "
                     f"{m['train']['max_drawdown']:.0%} | {m['holdout']['return']:+.0%} | "
                     f"{m['holdout']['max_drawdown']:.0%} | {full_ret} | {vs} | "
                     f"{result['trade_count'] if result else ''} |")

    def feature_table(rows):
        out = ["| Characteristic | Lift | Models | In top group |", "|---|---|---|---|"]
        out += [f"| `{f}` | {l} | {n} | {t} |" for f, l, n, t in rows]
        return out

    lines += ["", f"## Characteristics of the best {market['product']} models", "",
              "Lift is how much more often a characteristic appears in the top-scoring models than "
              "overall (1.0 = no effect), shrunk towards 1 when it is rare.", ""]
    lines += feature_table(final["market"]["best"])
    lines += ["", "### Characteristics to avoid", ""] + feature_table(final["market"]["worst"])
    lines += ["", "### Strongest single conditions", ""] + feature_table(final["market_atoms"])
    if first.get("reference"):
        lines += ["", "## Reference: what worked in the existing models", "",
                  f"From {len(harvest.genomes)} reference profiles; these weights seeded generation 0.", ""]
        lines += feature_table(first["reference"]["best"])
    if harvest.skipped:
        lines += ["", f"{len(harvest.skipped)} reference profiles were outside the search space and only lent their "
                  "conditions: " + ", ".join(name for name, _ in harvest.skipped) + "."]
    if ranking is not None:
        from model_assessor import format_table
        ranked_all, discarded, source = ranking
        lines += ["", f"## Ranking of every {market['product']} model", "",
                  f"Against {source}, after backtesting all {len(ranked_all)} under the same conditions. "
                  f"{sum(a['passes'] for a in ranked_all)} pass; {len(discarded)} moved to `config/discard/`.",
                  "", "```text", format_table(ranked_all, discarded), "```"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

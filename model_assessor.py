"""Assess models against your goals, rank a currency's models, and set the weakest aside.

Goals live in a private file, search_config/goals.json by default (start from the template
in search_config/.example/goals.example.json). It can set any of three kinds of target, and
every target left out or set to null is ignored:

    growth        minimum annual growth, growth multiple, or margin over buy and hold
    ending_value  minimum ending value
    risk          maximum drawdown, maximum drawdown within the drawdown window, minimum
                  trade count, minimum win rate
    recent        targets for a recent period only, from its `start` date: minimum return,
                  minimum growth relative to buy and hold, maximum drawdown, minimum trades.
                  Every model is backtested from that date (with live fees, never logged), so
                  a model whose full history hides a weak recent record can't pass.

    python model_assessor.py                          # every model in config/, from the performance log
    python model_assessor.py --product DOGE-USD       # one currency's models
    python model_assessor.py config/doge_0*.json      # chosen profiles
    python model_assessor.py --product DOGE-USD --rerun     # backtest them first (in parallel)
    python model_assessor.py --product DOGE-USD --discard   # rank, then move the bottom 80% aside
    python model_assessor.py --restore doge_042 doge_107    # move discarded models back

Ranking puts the models that pass first, then those meeting more targets, then the higher
score: annualized log growth plus `drawdown_weight` x log(1 - max drawdown), the search's
fitness. Discarding always backtests every model of the currency first, under the same
conditions, so the ranking compares like with like. The bottom `discard_fraction` move to
config/discard/, where the simulator no longer lists them, and each move is recorded in
config/discard/discard_log.csv. model_search.py runs the same ranking after every search.
"""

import argparse
import csv
import fnmatch
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from model_library import (CONFIG_DIR, DISCARD, DISCARD_LOG, LOCK_NAME, append_discard_log, currency_folder,
                           currency_folders, discard_folder, discard_folders, library_root, models_lock, organize,
                           profile_paths, read_profile)
from performance_log import DEFAULT_PATH as PERFORMANCE_LOG, profile_fingerprint

ROOT = Path(__file__).parent
DISCARD_DIR = CONFIG_DIR / DISCARD  # the folder name each currency folder's discard folder shares
MODELS_LOCK = CONFIG_DIR / LOCK_NAME
DEFAULT_GOALS = ROOT / "search_config" / "goals.json"
EXAMPLE_GOALS = ROOT / "search_config" / ".example" / "goals.example.json"
ASSESSMENT_DIR = ROOT / "data" / "assessments"

# (section, key, metric, comparison, label): every target a goals file can set.
TARGETS = [
    ("growth", "min_annual_growth_pct", "annual_growth_pct", ">=", "annual growth %"),
    ("growth", "min_growth_multiple", "growth_multiple", ">=", "growth multiple"),
    ("growth", "min_vs_buy_and_hold_pct", "vs_buy_and_hold_pct", ">=", "vs buy and hold %"),
    ("ending_value", "min_ending_value", "ending_value", ">=", "ending value"),
    ("risk", "max_drawdown_pct", "max_drawdown_pct", "<=", "max drawdown %"),
    ("risk", "max_worst_in_window_pct", "worst_in_window_pct", "<=", "worst drawdown in window %"),
    ("risk", "min_trades", "trades", ">=", "trades"),
    ("risk", "min_win_rate_pct", "win_rate_pct", ">=", "win rate %"),
]
# (key, metric, comparison, label): the targets a goals file's "recent" section can set.
RECENT_TARGETS = [
    ("min_return_pct", "return_pct", ">=", "recent return %"),
    ("min_vs_buy_and_hold_ratio", "vs_buy_and_hold_ratio", ">=", "recent growth / buy and hold growth"),
    ("max_drawdown_pct", "max_drawdown_pct", "<=", "recent max drawdown %"),
    ("min_trades", "trades", ">=", "recent trades"),
]
RANKING_DEFAULTS = {"drawdown_weight": 0.5, "discard_fraction": 0.8, "min_keep": 10, "protect": []}


class GoalsError(ValueError):
    pass


def load_goals(path=DEFAULT_GOALS):
    """Read and check a goals file; returns it with the ranking defaults filled in."""
    path = Path(path)
    if not path.exists():
        raise GoalsError(f"No goals at {path}. Copy the template to start:\n"
                         f"    cp {EXAMPLE_GOALS.relative_to(ROOT).as_posix()} {path.as_posix()}")
    with path.open(encoding="utf-8") as handle:
        goals = json.load(handle)
    return check_goals(goals, path)


def check_goals(goals, source="goals"):
    known = {(section, key) for section, key, *_ in TARGETS}
    problems = []
    for section in ("growth", "ending_value", "risk"):
        for key, value in (goals.get(section) or {}).items():
            if key.startswith("_") or key == "description":
                continue
            if (section, key) not in known:
                problems.append(f"unknown target {section}.{key}")
            elif value is not None and not isinstance(value, (int, float)):
                problems.append(f"{section}.{key} must be a number or null")
    recent = goals.get("recent") or {}
    if recent:
        try:
            datetime.strptime(str(recent.get("start")), "%Y-%m-%d")
        except ValueError:
            problems.append("recent.start must be a date such as 2025-04-01")
        known_recent = {key for key, *_ in RECENT_TARGETS} | {"start", "score_weight", "description"}
        for key, value in recent.items():
            if key.startswith("_") or key in ("start", "description"):
                continue
            if key not in known_recent:
                problems.append(f"unknown target recent.{key}")
            elif value is not None and not isinstance(value, (int, float)):
                problems.append(f"recent.{key} must be a number or null")
    if goals.get("require", "all") not in ("all", "any"):
        problems.append('require must be "all" or "any"')
    ranking = {**RANKING_DEFAULTS, **(goals.get("ranking") or {})}
    if not 0 <= ranking["discard_fraction"] < 1:
        problems.append("ranking.discard_fraction must be at least 0 and below 1")
    if problems:
        raise GoalsError(f"{source}: " + "; ".join(problems))
    return {**goals, "require": goals.get("require", "all"), "ranking": ranking}


def set_targets(goals):
    """The full-history targets the goals file sets: (key, metric, comparison, value, label)."""
    return [(key, metric, comparison, goals[section][key], label)
            for section, key, metric, comparison, label in TARGETS
            if (goals.get(section) or {}).get(key) is not None]


def recent_targets(goals):
    """The recent-period targets the goals file sets, in the same form as set_targets."""
    recent = goals.get("recent") or {}
    return [(key, metric, comparison, recent[key], label)
            for key, metric, comparison, label in RECENT_TARGETS if recent.get(key) is not None]


def recent_start(goals):
    """The recent period's start as a unix timestamp, or None when the goals set no recent period."""
    recent = goals.get("recent") or {}
    if not recent.get("start"):
        return None
    return int(datetime.strptime(recent["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _number(row, column):
    try:
        return float(row[column])
    except (KeyError, TypeError, ValueError):
        return None


def row_metrics(row, drawdown_weight=RANKING_DEFAULTS["drawdown_weight"]):
    """The measures goals are checked against, from a performance-log row (see performance_log.py)."""
    start = datetime.strptime(row["test_start"], "%Y-%m-%d %H:%M")
    end = datetime.strptime(row["test_end"], "%Y-%m-%d %H:%M")
    years = max((end - start).total_seconds() / (365.25 * 86400), 0.25)
    growth = max(_number(row, "growth_multiple") or 0.0, 1e-9)
    drawdown = _number(row, "max_drawdown_pct") or 0.0
    return {
        "years": years,
        "annual_growth_pct": (growth ** (1 / years) - 1) * 100,
        "growth_multiple": growth,
        "vs_buy_and_hold_pct": _number(row, "vs_buy_and_hold_pct"),
        "ending_value": _number(row, "ending_value"),
        "max_drawdown_pct": drawdown,
        "worst_in_window_pct": _number(row, "worst_in_window_pct"),
        "trades": _number(row, "trades"),
        "win_rate_pct": _number(row, "win_rate_pct"),
        "score": (math.log(growth) + drawdown_weight * math.log(max(1 - drawdown / 100, 1e-9))) / years,
    }


def result_metrics(result, drawdown_weight=RANKING_DEFAULTS["drawdown_weight"]):
    """The recent-period measures, from a backtest result (see run_backtest)."""
    years = max((result["period_end"] - result["period_start"]) / (365.25 * 86400), 0.25)
    growth = max(1 + result["total_return"], 1e-9)
    held = 1 + result["buy_and_hold_return"]
    drawdown = result["max_drawdown"]
    return {"return_pct": result["total_return"] * 100,
            "vs_buy_and_hold_ratio": growth / held if held > 0 else None,
            "max_drawdown_pct": drawdown * 100, "trades": result["trade_count"],
            "score": (math.log(growth) + drawdown_weight * math.log(max(1 - drawdown, 1e-9))) / years}


def _check(targets, metrics, met, missed):
    for key, metric, comparison, value, label in targets:
        actual = metrics.get(metric)
        ok = actual is not None and (actual >= value if comparison == ">=" else actual <= value)
        (met if ok else missed).append(
            f"{label} {comparison} {value:g}" + ("" if actual is None else f" (got {actual:,.4g})"))


def assess(name, row, goals, recent=None):
    """How one model's results measure up to `goals`; `row` None means it has no logged results.

    `recent` is the model's backtest result from the goals' recent start (None if it has none).
    With a recent period, its targets count like the others, and `score_weight` times its
    score is added to the model's score.
    """
    targets, later = set_targets(goals), recent_targets(goals)
    if row is None:
        return {"model": name, "product": "", "passes": False, "goals_met": -1, "goals_set": len(targets) + len(later),
                "score": -math.inf, "metrics": {}, "recent": {}, "met": [], "missed": ["no results"]}
    weight = goals["ranking"]["drawdown_weight"]
    metrics = row_metrics(row, weight)
    met, missed = [], []
    _check(targets, metrics, met, missed)
    score, recent_metrics = metrics["score"], {}
    if recent_start(goals) is not None:
        if recent is None:
            missed.extend(f"{label}: no recent results" for *_, label in later)
            score = -math.inf if later else score
        else:
            recent_metrics = result_metrics(recent, weight)
            _check(later, recent_metrics, met, missed)
            score += (goals.get("recent") or {}).get("score_weight", 0) * recent_metrics["score"]
    passes = not missed if goals["require"] == "all" else (bool(met) or not targets + later)
    return {"model": name, "product": row.get("product", ""), "passes": passes, "goals_met": len(met),
            "goals_set": len(targets) + len(later), "score": score, "metrics": metrics, "recent": recent_metrics,
            "met": met, "missed": missed}


def rank(assessments):
    """Best first: passing models, then more targets met, then the higher score."""
    return sorted(assessments, key=lambda a: (a["passes"], a["goals_met"], a["score"]), reverse=True)


# --- the models and their results ----------------------------------------------------

def config_models(product=None, paths=None, config_dir=CONFIG_DIR):
    """[(name, path, profile)] for the profiles given, or every profile in the library: config/ and its
    currency folders (see model_library.py). With `product`, only that currency's models."""
    paths = profile_paths(config_dir) if paths is None else [Path(p) for p in paths]
    models = []
    for path in paths:
        profile = read_profile(path)
        if profile is None or (product and profile.get("product_id") != product):
            continue
        models.append((path.stem, path, profile))
    return models


def log_rows(path=PERFORMANCE_LOG):
    if not Path(path).exists():
        return {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return {row["model"]: row for row in csv.DictReader(handle)}


def rerun(models, friction_inputs, client=None, workers=None, performance_path=PERFORMANCE_LOG, progress=None):
    """Backtest the models in parallel, recording them in the performance log; returns {name: error}."""
    from parallel_backtest import run_parallel
    run = run_parallel(client, [(name, profile) for name, _, profile in models], workers=workers,
                       friction_inputs=friction_inputs, performance_path=performance_path, progress=progress,
                       keep_heavy=False)
    return run["errors"]


def recent_results(models, goals, friction_inputs, client=None, workers=None, progress=None):
    """{name: result} for the models backtested from the goals' recent start, in parallel and never logged."""
    start = recent_start(goals)
    if start is None:
        return {}
    from parallel_backtest import run_parallel
    run = run_parallel(client, [(name, profile) for name, _, profile in models], workers=workers,
                       start_time=start, friction_inputs=friction_inputs, performance_path=None,
                       progress=progress, keep_heavy=False)
    return {name: result for (name, _, _), result in zip(models, run["results"]) if result is not None}


def assess_models(models, goals, rows, recent=None):
    """Assess (name, path, profile) models from log rows (and `recent` results from recent_results);
    a row for an edited profile doesn't count."""
    recent = recent or {}
    out = []
    for name, path, profile in models:
        row = rows.get(name)
        if row is not None and row.get("profile_fingerprint") not in ("", None, profile_fingerprint(profile)):
            row = None
        out.append({**assess(name, row, goals, recent.get(name)), "path": path,
                    "product": profile.get("product_id", "")})
    return out


# --- discarding ----------------------------------------------------------------------

def behaviour(assessment):
    """What a model did over the full history; two models with the same behaviour traded identically."""
    m = assessment["metrics"]
    return tuple(None if m.get(k) is None else round(m[k], 6)
                 for k in ("growth_multiple", "max_drawdown_pct", "trades", "win_rate_pct", "worst_in_window_pct"))


def mark_duplicates(ranked):
    """Mark each model that only repeats a better-ranked model's trades with assessment["duplicate_of"]."""
    first = {}
    for assessment in ranked:
        if not assessment["metrics"]:
            continue
        key = (assessment["product"], behaviour(assessment))
        if key in first:
            assessment["duplicate_of"] = first[key]
            assessment["missed"] = assessment["missed"] + [f"duplicate of {first[key]}"]
        else:
            first[key] = assessment["model"]
    return ranked


def to_discard(ranked, ranking):
    """The models to set aside under the goals' ranking settings: every duplicate (see mark_duplicates),
    then the bottom `discard_fraction` of the distinct models, keeping at least `min_keep` of them."""
    protect = ranking.get("protect") or []
    protected = lambda a: any(fnmatch.fnmatch(a["model"], pattern) for pattern in protect)
    distinct = [a for a in ranked if not a.get("duplicate_of")]
    total = len(distinct)
    keep = max(total - math.floor(total * ranking["discard_fraction"]), min(ranking["min_keep"], total))
    chosen = [a for a in ranked if a.get("duplicate_of")] + distinct[keep:]
    return [a for a in chosen if not protected(a)]


def discard(assessments, total, config_dir=CONFIG_DIR, now=None):
    """Move these models' profiles to their currency's discard folder and log why; returns the names moved.

    `total` is how many models were ranked. Each model goes to config/<product>/discard/, and
    its move is recorded in that folder's discard_log.csv. A profile already gone (moved by
    another run) is skipped. A name already in the discard folder gets a numbered suffix there.
    """
    config_dir = Path(config_dir)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    moved, logs = [], {}
    with models_lock(config_dir):
        for assessment in assessments:
            source = Path(assessment.get("path") or config_dir / f"{assessment['model']}.json")
            if not source.exists():
                continue
            product = assessment["product"] or (read_profile(source) or {}).get("product_id", "")
            folder = discard_folder(product, config_dir) if product else config_dir / DISCARD
            folder.mkdir(parents=True, exist_ok=True)
            target, number = folder / source.name, 2
            while target.exists():
                target, number = folder / f"{source.stem}_{number}.json", number + 1
            shutil.move(source, target)
            logs.setdefault(folder, []).append({
                "discarded_utc": stamp, "model": assessment["model"], "product": product,
                "rank": assessment.get("rank", ""), "ranked": total, "passes": "yes" if assessment["passes"] else "no",
                "goals_met": assessment["goals_met"], "goals_set": assessment["goals_set"],
                "score": f"{assessment['score']:.4f}", "missed": "; ".join(assessment["missed"]), "file": target.name})
            moved.append(assessment["model"])
        for folder, rows in logs.items():
            append_discard_log(folder, rows)
    return moved


def restore(names, config_dir=CONFIG_DIR):
    """Move discarded models back from any discard folder into their currency's folder; returns
    (restored, problems)."""
    config_dir = Path(config_dir)
    restored, problems = [], []
    with models_lock(config_dir):
        for name in names:
            source = next((folder / f"{name}.json" for folder in discard_folders(config_dir)
                           if (folder / f"{name}.json").exists()), None)
            if source is None:
                problems.append(f"{name}: not in any discard folder")
                continue
            product = (read_profile(source) or {}).get("product_id")
            home = currency_folder(product, config_dir) if product else config_dir
            target = home / source.name
            if target.exists():
                problems.append(f"{name}: {home} already has a model with that name")
            else:
                home.mkdir(parents=True, exist_ok=True)
                shutil.move(source, target)
                restored.append(name)
    return restored, problems


def below_buy_and_hold(models, rows):
    """Assessments, for discard(), of the models whose logged full-history result ends below buy and hold.

    Buy and hold starts where the model could first buy, as in the simulator's comparison. A
    model without a current row (none logged, or logged for an earlier version of its profile)
    is left out: there is nothing to judge it on.
    """
    out = []
    for name, path, profile in models:
        row = rows.get(name)
        if row is None or row.get("profile_fingerprint") not in ("", None, profile_fingerprint(profile)):
            continue
        versus = _number(row, "vs_buy_and_hold_pct")
        if versus is None or versus >= 0:
            continue
        out.append({"model": name, "path": path, "product": profile.get("product_id", ""), "passes": False,
                    "goals_met": 0, "goals_set": 1, "score": versus / 100, "rank": "",
                    "missed": [f"worse than buy and hold ({versus:+.1f}% vs buy and hold, growth "
                               f"{_number(row, 'growth_multiple') or 0:.2f}x against "
                               f"{_number(row, 'buy_and_hold_multiple') or 0:.2f}x)"]})
    return out


def discard_below_buy_and_hold(friction_inputs, client=None, workers=None, product=None, config_dir=CONFIG_DIR,
                               performance_path=PERFORMANCE_LOG, apply=True, progress=None, say=print):
    """Backtest every model (of `product`, or all) with the current code, then move those ending below buy and
    hold to their currency's discard folder. Returns (models judged, the losers' assessments)."""
    models = config_models(product, config_dir=config_dir)
    if not models:
        say("No models to judge")
        return [], []
    say(f"Backtesting {len(models)} models with the current code to compare each with buy and hold")
    for name, error in rerun(models, friction_inputs, client, workers, performance_path, progress).items():
        say(f"  {name} failed: {error}")
    losers = below_buy_and_hold(models, log_rows(performance_path))
    if apply and losers:
        moved = discard(losers, len(models), config_dir)
        say(f"Moved {len(moved)} of {len(models)} models that ended below buy and hold to their currency's "
            f"discard folder (see {DISCARD_LOG} there)")
    return models, losers


def rank_and_discard(product, goals, friction_inputs, client=None, workers=None, config_dir=CONFIG_DIR,
                     performance_path=PERFORMANCE_LOG, apply=True, progress=None, say=print):
    """Backtest every model of `product` alike, rank them, and (with `apply`) move the bottom aside.

    Returns (ranked assessments, names discarded or to discard).
    """
    models = config_models(product, config_dir=config_dir)
    if not models:
        say(f"No {product} models in {config_dir}")
        return [], []
    say(f"Ranking {len(models)} {product} models: backtesting them under the same conditions")
    errors = rerun(models, friction_inputs, client, workers, performance_path, progress)
    for name, error in errors.items():
        say(f"  {name} failed: {error}")
    rows = log_rows(performance_path)
    for name in errors:
        rows.pop(name, None)  # a model that no longer runs ranks last, whatever it logged before
    recent = {}
    if recent_start(goals) is not None:
        say(f"Backtesting them from {goals['recent']['start']} for the recent-period targets")
        recent = recent_results(models, goals, friction_inputs, client, workers, progress)
    ranked = mark_duplicates(rank(assess_models(models, goals, rows, recent)))
    for position, assessment in enumerate(ranked, start=1):
        assessment["rank"] = position
    chosen = to_discard(ranked, goals["ranking"])
    if apply and chosen:
        moved = discard(chosen, len(ranked), config_dir)
        say(f"Moved the bottom {len(moved)} of {len(ranked)} {product} models to "
            f"{discard_folder(product, config_dir)} (see {DISCARD_LOG} there)")
        return ranked, moved
    return ranked, [a["model"] for a in chosen]


# --- report --------------------------------------------------------------------------

def _value(metrics, key, form):
    value = metrics.get(key)
    return "" if value is None else format(value, form)


def format_table(ranked, discarded=()):
    discarded = set(discarded)
    with_recent = any(a.get("recent") for a in ranked)
    lines = [f"{'#':>4} {'model':32s} {'pass':4s} {'goals':>5s} {'annual':>8s} {'ending value':>14s} "
             f"{'max dd':>7s} {'trades':>6s} {'score':>7s}" + (f" {'recent':>8s}" if with_recent else "")]
    for position, a in enumerate(ranked, start=1):
        m = a["metrics"]
        goals = f"{a['goals_met']}/{a['goals_set']}" if a["goals_met"] >= 0 else "-"
        recent = (f" {_value(a.get('recent') or {}, 'return_pct', '+.1f'):>7s}%" if with_recent else "")
        lines.append(f"{position:>4} {a['model'][:32]:32s} {'yes' if a['passes'] else 'no':4s} {goals:>5s} "
                     f"{_value(m, 'annual_growth_pct', '+.1f'):>7s}% {_value(m, 'ending_value', ',.0f'):>14s} "
                     f"{_value(m, 'max_drawdown_pct', '.1f'):>6s}% {_value(m, 'trades', '.0f'):>6s} "
                     f"{a['score']:+7.3f}{recent}" + ("  -> discard" if a["model"] in discarded else "")
                     + (f"  (= {a['duplicate_of']})" if a.get("duplicate_of") else ""))
    return "\n".join(lines)


def write_report(ranked, goals_path, discarded=(), folder=ASSESSMENT_DIR):
    """Every assessment as a CSV in data/assessments/; returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"assessment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    metrics = ["annual_growth_pct", "growth_multiple", "vs_buy_and_hold_pct", "ending_value", "max_drawdown_pct",
               "worst_in_window_pct", "trades", "win_rate_pct", "years"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        recent_columns = ["return_pct", "vs_buy_and_hold_ratio", "max_drawdown_pct", "trades"]
        writer.writerow(["rank", "model", "product", "passes", "goals_met", "goals_set", "score", *metrics,
                         *(f"recent_{k}" for k in recent_columns), "met", "missed", "discarded", "goals_file"])
        for position, a in enumerate(ranked, start=1):
            writer.writerow([position, a["model"], a["product"], "yes" if a["passes"] else "no", a["goals_met"],
                             a["goals_set"], f"{a['score']:.4f}",
                             *(("" if a["metrics"].get(k) is None else f"{a['metrics'][k]:.6g}") for k in metrics),
                             *(("" if (a.get("recent") or {}).get(k) is None else f"{a['recent'][k]:.6g}")
                               for k in recent_columns),
                             "; ".join(a["met"]), "; ".join(a["missed"]),
                             "yes" if a["model"] in set(discarded) else "", goals_path])
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("profiles", nargs="*", help="profiles to assess (default: every model in config/)")
    parser.add_argument("--goals", default=str(DEFAULT_GOALS), help="goals file (default: search_config/goals.json)")
    parser.add_argument("--product", help="only this currency's models, such as DOGE-USD")
    parser.add_argument("--rerun", action="store_true", help="backtest the models first instead of reading the log")
    parser.add_argument("--discard", action="store_true",
                        help="rank the --product's models (backtesting them all) and move the bottom aside")
    parser.add_argument("--preview", action="store_true", help="with --discard: list what would move, move nothing")
    parser.add_argument("--restore", nargs="+", metavar="MODEL",
                        help="move discarded models back into their currency's folder")
    parser.add_argument("--below-buy-and-hold", action="store_true",
                        help="backtest every model (or --product's) with the current code and move those ending "
                             "below buy and hold to their discard folder; --preview lists them instead")
    parser.add_argument("--organize", action="store_true",
                        help="move profiles at the top of config/ (and in config/discard/) into a folder per "
                             "currency, config/<product>/, then exit")
    parser.add_argument("--offline", action="store_true", help="when backtesting: cached candles and fallback fees")
    parser.add_argument("--workers", help="worker processes when backtesting: auto (default) or a number")
    parser.add_argument("--config-dir", default=str(CONFIG_DIR),
                        help="the folder of models to assess, rank, discard from and restore to (default: config/)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.below_buy_and_hold:
        from collections import Counter
        from parallel_backtest import market_inputs, parse_workers
        models = config_models(args.product, config_dir=args.config_dir)
        client, inputs = market_inputs([profile for _, _, profile in models], args.offline)
        judged, losers = discard_below_buy_and_hold(inputs, client, parse_workers(args.workers or "auto"),
                                                    args.product, args.config_dir, apply=not args.preview)
        totals, below = Counter(p.get("product_id") for _, _, p in judged), Counter(a["product"] for a in losers)
        for product in sorted(totals):
            print(f"  {product}: {below[product]} of {totals[product]} below buy and hold"
                  + (" (preview: nothing moved)" if args.preview else ""))
        return
    if args.organize:
        moved, skipped = organize(args.config_dir)
        print(f"Moved {len(moved)} profiles into currency folders"
              + "".join(f"\n  {name}: kept where it is ({reason})" for name, reason in skipped))
        return
    if args.restore:
        restored, problems = restore(args.restore, config_dir=args.config_dir)
        for name in restored:
            print(f"Restored {name}")
        for problem in problems:
            print(f"Not restored: {problem}")
        sys.exit(1 if problems else 0)
    try:
        goals = load_goals(args.goals)
    except GoalsError as exc:
        sys.exit(str(exc))
    if args.discard and not args.product:
        sys.exit("--discard ranks one currency's models: add --product, such as --product DOGE-USD")
    from parallel_backtest import market_inputs, parse_workers
    workers = parse_workers(args.workers or "auto")
    targets = set_targets(goals) + recent_targets(goals)
    print(f"Goals from {args.goals}: " + (", ".join(f"{label} {comparison} {value:g}"
                                                    for _, _, comparison, value, label in targets) or "none set")
          + f" (require {goals['require']})"
          + (f"; recent period from {goals['recent']['start']}" if recent_start(goals) is not None else ""))

    discarded = []
    if args.discard:
        models = config_models(args.product, config_dir=args.config_dir)
        client, inputs = market_inputs([profile for _, _, profile in models], args.offline)
        ranked, discarded = rank_and_discard(args.product, goals, inputs, client, workers, config_dir=args.config_dir,
                                             apply=not args.preview)
        if args.preview:
            print(f"Preview: {len(discarded)} of {len(ranked)} would move to "
                  f"{discard_folder(args.product, args.config_dir)}")
    else:
        models = config_models(args.product, args.profiles or None, config_dir=args.config_dir)
        if not models:
            sys.exit("No models to assess")
        recent = {}
        if args.rerun or recent_start(goals) is not None:
            client, inputs = market_inputs([profile for _, _, profile in models], args.offline)
            if args.rerun:
                for name, error in rerun(models, inputs, client, workers).items():
                    print(f"  {name} failed: {error}")
            recent = recent_results(models, goals, inputs, client, workers)
        ranked = mark_duplicates(rank(assess_models(models, goals, log_rows(), recent)))
    print(format_table(ranked, discarded))
    passing = sum(a["passes"] for a in ranked)
    print(f"\n{passing} of {len(ranked)} models pass. Report: {write_report(ranked, args.goals, discarded)}")


if __name__ == "__main__":
    main()

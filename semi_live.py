"""Semi-live mode: project where a currency's price could go, then watch it and paper-trade the models on it.

A semi-live session works in four steps (the simulator's "Semi-live monitoring" data mode runs them):

1. Each model is backtested on its market's history, as in a normal run.
2. Five directions the price could take over the monitoring period are projected for the target
   currency (DirectionOdds): its real moves over periods of the same length in recent history (the last
   `basis_days`, in hourly candles when the models' are coarser) split into five ranges, each with the
   median move in it and its share of history as the probability. Each model is then run on `paths`
   realistic synthetic paths per direction (build_projection: the market's own move size, with fat
   tails and calm and violent stretches, pinned to end on the direction's move), giving the return to
   expect from it if the price goes that way.
3. For the monitoring period, the live price is polled every `poll_seconds` and each candle is fetched
   as it closes. Every model trades the closed candles from the start of the session on paper, exactly
   as a backtest would (LiveSession.update_market): orders fill at the candle's close, stops intrabar.
4. Throughout, the session reports which direction the price is tracking most closely, how likely each
   direction is now (DirectionOdds.at), and each model's live return against the return its projection
   for the closest direction expected by now. The simulator draws the session on its Chart tab
   (draw_live). At the end, it
   writes a record of the session to data/semi_live/<time>/.

Nothing here places an order: the session only reads prices and candles.
"""

import csv
import json
import math
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import synthetic
from backtest import DEFAULT_CACHE_DIR, load_candles, lookback_window, resolve_granularity, run_backtest

# The five directions, strongest rise first. Each covers a fifth of the range of the historic moves (the
# outer two take the tails too), so the middle ones usually hold more of history: more probable.
DIRECTIONS = ((4, "Strong rally"), (3, "Rally"), (2, "Middle"), (1, "Decline"), (0, "Strong decline"))
DIRECTION_COLORS = ("#1a7f37", "#7cc68d", "#6e7781", "#f0a35e", "#cf222e")
RANGE_QUANTILES = (0.025, 0.975)  # the five ranges split the moves between these percentiles evenly
DEFAULT_SETTINGS = {"semi_live_currency": "", "semi_live_period": "1d", "semi_live_poll_seconds": 30,
                    "semi_live_paths": 5, "semi_live_basis_days": 365}
# Fetch a candle this long after it closes, so Coinbase has published it: a candle asked for too early would be
# recorded as fetched before it exists, and never fetched again.
SETTLE_SECONDS = 30
SESSION_DIR = Path(__file__).parent / "data" / "semi_live"
SEED = 2026
MIN_SAMPLES = 30  # fewer historic moves than this, and the directions come from a normal distribution instead
DURATION_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
CHART_POINTS = 400
# The directions and their odds come from candles this fine (or the models' own, when finer), so a session of
# hours is judged on hourly moves, not on whole days.
DETAIL_GRANULARITY = "ONE_HOUR"
LEAD_IN_DAYS = 7  # of those candles' closes the session keeps, to join a chart's history to the live price
# The live indicators are computed on this many of the latest candles (and the one still open): enough to warm up
# the slowest common ones, such as a 200-candle EMA, and quick to redo at every price check.
LIVE_WARMUP_CANDLES = 2000
VOLUME_CANDLES = 20  # the open candle's volume so far is guessed from the average of this many before it


class SemiLiveError(ValueError):
    pass


def parse_duration(text):
    """Seconds in a duration such as "90m", "12h", "1d" or "2w"."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", str(text).lower())
    if not match or float(match[1]) <= 0:
        raise SemiLiveError(f"The monitoring period {text!r} must be a duration such as 90m, 12h, 1d or 2w")
    return int(round(float(match[1]) * DURATION_SECONDS[match[2]]))


def format_duration(seconds):
    """A short duration such as "1d 4h", "3h 12m" or "45m"."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else f"{seconds}s"


def settings_of(options):
    """The semi-live settings in the simulator's options, checked: (currency, period seconds, poll, paths, basis)."""
    options = {**DEFAULT_SETTINGS, **(options or {})}
    period = parse_duration(options["semi_live_period"])
    try:
        poll = float(options["semi_live_poll_seconds"])
        paths = int(options["semi_live_paths"])
        basis = float(options["semi_live_basis_days"])
    except (TypeError, ValueError):
        raise SemiLiveError("The semi-live poll interval, paths and history days must be numbers") from None
    if poll < 5:
        raise SemiLiveError("Check the price every 5 seconds or more, to stay well under Coinbase's rate limit")
    if not 1 <= paths <= 100:
        raise SemiLiveError("Paths per direction must be from 1 to 100")
    if basis < 7:
        raise SemiLiveError("Base the directions on at least 7 days of history")
    return str(options["semi_live_currency"] or ""), period, poll, paths, basis


def pick_models(models, currency):
    """(the (name, profile) models that take part, the currencies they trade): every selected model and its
    currency when `currency` is blank, else only the models that trade `currency`."""
    chosen = [(name, profile) for name, profile in models if not currency or profile["product_id"] == currency]
    if not chosen:
        raise SemiLiveError(f"None of the selected models trade {currency}: select at least one {currency} model")
    return chosen, list(dict.fromkeys(profile["product_id"] for _, profile in chosen))


# --- the five directions -----------------------------------------------------------------

class DirectionOdds:
    """The five directions over `horizon_seconds` from the last candle, and how likely each is as the session goes.

    The market's real log-price moves over every stretch of the horizon's length within the last
    `basis_days` (overlapping, one starting at each candle) are split into five ranges of equal width
    between their 2.5th and 97.5th percentiles; the outer ranges take the tails. Each direction's `move`
    is the median move in its range, and its `probability` the share of moves in it. A horizon shorter
    than a candle scales the moves by the square root of the time, as for a random walk.

    at() updates the odds once the session is under way: each outcome is the live move so far plus how a
    historic stretch moved over the time left, and stretches that had moved like the live price by then
    count for more (a Gaussian kernel). So a price falling early shifts the odds towards the declines,
    and they settle on one direction only as the end nears. With too little history, the moves come from a normal
    distribution with the recent per-candle drift and spread, and so do the updated odds.
    """

    def __init__(self, candles, granularity_seconds, horizon_seconds, basis_days=365):
        count = max(1, int(round(horizon_seconds / granularity_seconds)))
        basis = int(basis_days * 86400 / granularity_seconds)
        closes = np.array([candle["close"] for candle in candles[-(basis + count + 1):]], dtype=float)
        closes = closes[closes > 0]
        if closes.size < 3:
            raise SemiLiveError("Too little price history to project directions from")
        self.count, self.horizon = count, horizon_seconds
        self.scale = math.sqrt(min(horizon_seconds / (count * granularity_seconds), 1.0))
        logs = np.log(closes)
        moves = (logs[count:] - logs[:-count]) * self.scale if logs.size > count else np.array([])
        if moves.size >= MIN_SAMPLES:
            self.logs, self.moves = logs, moves
            edges = np.linspace(*np.quantile(moves, RANGE_QUANTILES), 6)[1:-1]
            self.ranges = np.digitize(moves, edges)  # 0 (the deepest fall) .. 4 (the strongest rise)
            values = []
            for number, _ in DIRECTIONS:
                inside = moves[self.ranges == number]
                if inside.size:
                    values.append(float(np.median(inside)))
                else:  # no move fell in it: the middle of its range, or the edge of a tail
                    values.append(float(edges[0] if number == 0 else edges[-1] if number == 4
                                        else (edges[number - 1] + edges[number]) / 2))
            self.normal = None
            method = f"{moves.size:,} real {format_duration(horizon_seconds)} moves"
        else:
            steps = np.diff(logs)
            mean, spread = float(steps.mean()) * count, max(float(steps.std()) * math.sqrt(count) * self.scale, 1e-9)
            self.normal = statistics.NormalDist(mean, spread)
            edges = np.linspace(mean - 1.96 * spread, mean + 1.96 * spread, 6)[1:-1]
            bounds = [-math.inf, *edges, math.inf]
            values = []
            for number, _ in DIRECTIONS:
                low, high = (self.normal.cdf(x) if math.isfinite(x) else float(x > 0) for x in bounds[number:number + 2])
                values.append(self.normal.inv_cdf((low + high) / 2))
            method = "a normal distribution of the recent per-candle moves (too little history for real moves)"
        self.edges = [float(edge) for edge in edges]
        self.targets = [{"name": name, "move": float(value), "change": math.exp(float(value)) - 1, "method": method}
                        for (_, name), value in zip(DIRECTIONS, values)]
        for target, odds in zip(self.targets, self.at(0.0, 0.0)):
            target["probability"] = odds

    def _from_normal(self, mean, spread):
        bounds = [-math.inf, *self.edges, math.inf]
        if spread <= 0:
            return [float(bounds[number] <= mean < bounds[number + 1]) for number, _ in DIRECTIONS]
        dist = statistics.NormalDist(mean, spread)
        cdf = [0.0, *(dist.cdf(edge) for edge in self.edges), 1.0]
        return [cdf[number + 1] - cdf[number] for number, _ in DIRECTIONS]

    def at(self, elapsed_seconds, move_so_far):
        """The five directions' probabilities (strongest rise first, summing to 1) `elapsed_seconds` into the
        session, with the price `move_so_far` (a log move) from where it started."""
        fraction = min(max(elapsed_seconds / self.horizon, 0.0), 1.0)
        if self.normal is not None:
            mean, spread = self.normal.mean, self.normal.stdev
            return self._from_normal(move_so_far + mean * (1 - fraction), spread * math.sqrt(1 - fraction))
        # Each historic stretch, at the last whole candle the session has passed, has moved `so_far`; what it
        # moved after that, scaled to the time the session has left, is one way the rest could go. The live move
        # plus each of those is an outcome, weighted by how alike the stretch's start was to the live one.
        logs, count = self.logs, self.count
        starts = np.arange(self.moves.size)
        step = fraction * count
        whole = min(int(step), count)
        so_far = (logs[starts + whole] - logs[starts]) * self.scale
        left = math.sqrt((count - step) / (count - whole)) if whole < count else 0.0
        outcomes = move_so_far + (self.moves - so_far) * left
        spread = float(so_far.std())
        if whole == 0 or spread < 1e-12:
            weights = np.ones_like(so_far)
        else:
            width = 1.06 * spread * so_far.size ** -0.2
            log_weights = -0.5 * ((so_far - move_so_far * whole / step) / width) ** 2
            weights = np.exp(log_weights - log_weights.max())
        totals = np.bincount(np.digitize(outcomes, self.edges), weights, minlength=5)
        totals = totals / totals.sum()
        return [float(totals[number]) for number, _ in DIRECTIONS]


def projection_targets(candles, granularity_seconds, horizon_seconds, basis_days=365):
    """The five directions over `horizon_seconds`, strongest rise first (see DirectionOdds): a dict each, with
    its name, log `move`, price `change`, `probability` and the `method` behind them."""
    return DirectionOdds(candles, granularity_seconds, horizon_seconds, basis_days).targets


def projected_change(target, fraction):
    """The price change a direction expects `fraction` of the way through the period (straight in log terms)."""
    return math.exp(target["move"] * min(max(fraction, 0.0), 1.0)) - 1


def projection_lines(targets, anchor_time, anchor_price, horizon_seconds, points=60):
    """Each direction's line for a chart: {"time": [...], "lines": [[price, ...] per direction]}."""
    fractions = np.linspace(0, 1, points)
    return {"time": [anchor_time + fraction * horizon_seconds for fraction in fractions],
            "lines": [[anchor_price * (1 + projected_change(target, fraction)) for fraction in fractions]
                      for target in targets]}


def closest_direction(targets, anchor_time, anchor_price, horizon_seconds, ticks):
    """(index of the direction the observed prices track most closely, the distance to each), or (None, []).

    The distance is the root mean square of the gap, in log terms, between each observed price and the
    direction's line at that moment, so the whole session counts, not only the latest price.
    """
    seen = [(t, price) for t, price in ticks if t >= anchor_time and price > 0]
    if not seen or anchor_price <= 0:
        return None, []
    distances = []
    for target in targets:
        gaps = [math.log(price / anchor_price) - target["move"] * min((t - anchor_time) / horizon_seconds, 1.0)
                for t, price in seen]
        distances.append(math.sqrt(sum(gap * gap for gap in gaps) / len(gaps)))
    return int(np.argmin(distances)), distances


# --- each model on each direction ----------------------------------------------------------

def session_candles(granularity_seconds, started, horizon_seconds):
    """(start of the first candle the session trades, how many candles close during it).

    The first is the candle open when the session starts; the last, the final one to close by its end.
    """
    first = int(started) // granularity_seconds * granularity_seconds
    return first, max(0, int((started + horizon_seconds - first) // granularity_seconds))


def build_projection(history, granularity_seconds, target, count, direction=0, path=0, scale=1.0):
    """(candles, start): `history` with `count` synthetic candles appended that move `target["move"]` x `scale`.

    The candles move like the market's own (see synthetic._phase), with the same seed for every model on
    one market, so models are compared on the same paths.
    """
    per_day = 86400 / granularity_seconds
    _, spread, high_low, volume = synthetic._profile(history)
    rng = np.random.default_rng([SEED, direction, path, granularity_seconds])
    moves = synthetic._phase(target["move"] * scale, count, "even", spread, 1.0, per_day, rng)
    last = history[-1]
    start = last["start"] + granularity_seconds
    made = synthetic._candles(start, granularity_seconds, last["close"], moves, rng, spread, high_low, volume)
    return synthetic.SyntheticCandles(list(history) + made, synthetic.MemoryMetrics()), start


def _downsample(values, points=CHART_POINTS):
    step = max(1, math.ceil(len(values) / points))
    picked = list(values[::step])
    if values and picked[-1] != values[-1]:
        picked.append(values[-1])
    return picked


def _curve(result, granularity_seconds):
    """A result's value as a return at each candle close: {"time": [...], "return": [...]}."""
    series, capital = result["series"], result["starting_capital"]
    pairs = list(zip(series["time"], series["equity"]))
    pairs = _downsample(pairs)
    return {"time": [t + granularity_seconds for t, _ in pairs], "return": [value / capital - 1 for _, value in pairs]}


def project_model(history, granularity_seconds, profile, friction, target, count, direction, path, scale=1.0,
                  wait_for_fresh_signal=False):
    """One model on one path of one direction: its return, drawdown, trades and value curve."""
    candles, start = build_projection(history, granularity_seconds, target, count, direction, path, scale)
    result = run_backtest(candles, profile, friction=friction, start_time=start,
                          wait_for_fresh_signal=wait_for_fresh_signal)
    return {"direction": direction, "path": path, "total_return": result["total_return"],
            "buy_and_hold": result["buy_and_hold_from_test_start"], "max_drawdown": result["max_drawdown"],
            "trade_count": result["trade_count"], "curve": _curve(result, granularity_seconds)}


_WORKER_HISTORY = {}  # in a worker process: (market, before) -> history, read once per market


def _project_task(cache_dir, market, before, name, profile, friction, target, count, direction, path, scale, wait):
    """project_model in a worker process, reading the market's history from the candle store."""
    from backtest import load_cached_candles
    key = (market, before)
    if key not in _WORKER_HISTORY:
        loaded = load_cached_candles(cache_dir, *market, read_only=True)
        if loaded is None:
            raise SemiLiveError(f"{market[0]} {market[1]} has no stored candles")
        _WORKER_HISTORY.clear()
        _WORKER_HISTORY[key] = [candle for candle in loaded[0] if candle["start"] < before]
    _, granularity_seconds = resolve_granularity(market[1])
    return name, project_model(_WORKER_HISTORY[key], granularity_seconds, profile, friction, target, count,
                               direction, path, scale, wait)


def summarize_paths(runs):
    """One direction's runs: the median, worst and best return, and the curve of the median run."""
    ordered = sorted(runs, key=lambda run: (run["total_return"], run["path"]))  # path: the same pick in any order
    median = ordered[(len(ordered) - 1) // 2]
    return {"median": statistics.median(run["total_return"] for run in runs), "worst": ordered[0]["total_return"],
            "best": ordered[-1]["total_return"], "paths": len(runs),
            "buy_and_hold": statistics.median(run["buy_and_hold"] for run in runs),
            "trades": statistics.median(run["trade_count"] for run in runs), "curve": median["curve"]}


def run_projections(models, frictions, candles_by_market, targets, started, horizon_seconds, paths=5, workers=1,
                    wait_for_fresh_signal=False, progress=None, cache_dir=DEFAULT_CACHE_DIR):
    """Every model on `paths` paths of every direction: {model name: [summarize_paths per direction] or None}.

    A model's market whose candles are longer than the session (none closes during it) gets None. With
    more than one of `workers`, the runs share worker processes, each reading its market's history once.
    """
    tasks, out = [], {}
    for name, profile in models:
        granularity, granularity_seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        market = (profile["product_id"], granularity)
        history = candles_by_market[market]
        first, count = session_candles(granularity_seconds, started, horizon_seconds)
        if count == 0:
            out[name] = None
            continue
        # The model's candles span a little more or less than the period, so the move is scaled to fit.
        scale = count * granularity_seconds / horizon_seconds
        before = min(first, history[-1]["start"] + granularity_seconds)
        out[name] = [[] for _ in targets]
        for direction, target in enumerate(targets):
            for path in range(paths):
                tasks.append((market, before, name, profile, frictions[name], target, count, direction, path, scale))
    done = 0

    def finished(name, run):
        nonlocal done
        out[name][run["direction"]].append(run)
        done += 1
        if progress is not None:
            progress(done / len(tasks), f"{done:,} of {len(tasks):,} projected runs done ({name})")

    if workers > 1 and len(tasks) > 1:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from parallel_backtest import single_threaded_children
        single_threaded_children()
        with ProcessPoolExecutor(max_workers=min(workers, len(tasks)),
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(_project_task, str(cache_dir), *task, wait_for_fresh_signal) for task in tasks]
            for future in as_completed(futures):
                finished(*future.result())
    else:
        histories = {}
        for market, before, name, profile, friction, target, count, direction, path, scale in tasks:
            if (market, before) not in histories:
                histories[(market, before)] = [c for c in candles_by_market[market] if c["start"] < before]
            _, granularity_seconds = resolve_granularity(market[1])
            finished(name, project_model(histories[(market, before)], granularity_seconds, profile, friction, target,
                                         count, direction, path, scale, wait_for_fresh_signal))
    return {name: None if runs is None else [summarize_paths(direction) for direction in runs]
            for name, runs in out.items()}


def expected_by(curve, moment):
    """The return a projected curve had reached by `moment` (0 before its first candle closes)."""
    if not curve["time"] or moment < curve["time"][0]:
        return 0.0
    index = int(np.searchsorted(curve["time"], moment, side="right")) - 1
    return curve["return"][min(index, len(curve["return"]) - 1)]


# --- watching the market ----------------------------------------------------------------------

def latest_price(client, product_id):
    """The product's last traded price from Coinbase, or None if it can't be read."""
    try:
        product = client.get_product(product_id=product_id)
    except Exception:  # the session goes on with candle closes alone
        return None
    price = getattr(product, "price", None)
    if price is None and hasattr(product, "get"):
        price = product.get("price")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


class LiveSession:
    """One semi-live session: the directions, each model's projections and its paper trading since the start.

    `models` are (name, profile); `candles_by_market` the history each market's models were backtested
    on; `historic` each model's historic backtest result, by name; `projections` from run_projections.
    """

    def __init__(self, product_id, models, frictions, candles_by_market, targets, projections, started,
                 horizon_seconds, anchor_price, historic=None, wait_for_fresh_signal=False, settings="",
                 odds=None, lead_in=None):
        self.product_id, self.models, self.frictions = product_id, list(models), frictions
        self.targets, self.projections, self.historic = targets, projections, historic or {}
        self.started, self.horizon, self.ends = started, horizon_seconds, started + horizon_seconds
        self.anchor_price, self.wait, self.settings = anchor_price, wait_for_fresh_signal, settings
        self.ticks = [(started, anchor_price)]
        self.markets = {}  # (product, granularity) -> {"seconds", "first", "last" (last closed candle start)}
        self.live = {name: None for name, _ in self.models}
        self.notes = {}
        self.candles = {}  # each market's latest closed candles, for the live indicators
        self._compiled = {}  # each model's CompiledModel, made once
        for name, profile in self.models:
            granularity, seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
            market = (profile["product_id"], granularity)
            if market not in self.markets:
                history = candles_by_market[market]
                first, _ = session_candles(seconds, started, horizon_seconds)
                self.markets[market] = {"seconds": seconds, "first": first, "last": history[-1]["start"],
                                        "history_last": history[-1]["start"]}
                self.candles[market] = list(history[-LIVE_WARMUP_CANDLES:])
        self.odds = odds  # a DirectionOdds, to update each direction's probability as the price moves
        # The prices leading into the session (candles, granularity seconds), so a chart's history runs on into
        # the live price without a gap: the last LEAD_IN_DAYS of each candle's close, at its close.
        candles, seconds = lead_in or (None, None)
        self.lead_in = [] if not candles else [
            (c["start"] + seconds, c["close"]) for c in candles
            if started - LEAD_IN_DAYS * 86400 <= c["start"] + seconds <= started]

    def add_tick(self, moment, price):
        if price is not None and price > 0:
            self.ticks.append((moment, float(price)))

    def due(self, market, now):
        """Whether a new candle of `market` has closed (and settled) since the last one fetched."""
        info = self.markets[market]
        open_candle = int(now - SETTLE_SECONDS) // info["seconds"] * info["seconds"]
        return open_candle - info["seconds"] > info["last"] and not self._has_final(info)

    def _has_final(self, info):
        """Whether the market's last candle closing within the session has been fetched."""
        return info["last"] + 2 * info["seconds"] > self.ends

    def caught_up(self):
        """Whether every market's last candle of the session has been fetched."""
        return all(self._has_final(info) for info in self.markets.values())

    def update_market(self, market, candles):
        """Paper-trade every model on `market` over its candles that closed during the session so far."""
        info = self.markets[market]
        if candles:
            info["last"] = max(info["last"], candles[-1]["start"])
            self.candles[market] = list(candles[-LIVE_WARMUP_CANDLES:])
        within = [c for c in candles if c["start"] + info["seconds"] <= self.ends]
        if hasattr(candles, "metrics") and len(within) != len(candles):
            within = type(candles)(within, candles.metrics)
        session = [c for c in within if c["start"] >= info["first"]]
        if session:
            self.add_tick(session[-1]["start"] + info["seconds"], session[-1]["close"])
        for name, profile in self.models:
            granularity, _ = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
            if (profile["product_id"], granularity) != market or not session:
                continue
            try:
                result = run_backtest(within, profile, friction=self.frictions[name], start_time=info["first"],
                                      wait_for_fresh_signal=self.wait)
            except Exception as exc:  # one model's failure mustn't stop the session
                self.notes[name] = str(exc)
                continue
            self.notes.pop(name, None)
            self.live[name] = {"total_return": result["total_return"], "ending_equity": result["ending_equity"],
                               "buy_and_hold": result["buy_and_hold_from_test_start"],
                               "trade_count": result["trade_count"], "open_position": result["open_position"],
                               "max_drawdown": result["max_drawdown"], "candles": result["candle_count"],
                               "trades": result["trades"][-20:], "curve": _curve(result, info["seconds"])}

    def _forming(self, market, now):
        """The candle of `market` still open at `now`, as far as it has gone: it opens at the last close, its high
        and low are the extremes of the prices seen in it (the lead-in and the live checks), it closes at the
        latest price, and its volume is the recent average for the share of the candle gone. None when the
        candle before it hasn't been fetched yet, or no price has been seen in it."""
        seconds, candles = self.markets[market]["seconds"], self.candles[market]
        start = int(now) // seconds * seconds
        if not candles or candles[-1]["start"] + seconds != start:
            return None
        prices = [p for t, p in self.lead_in if start < t <= self.started] + [p for t, p in self.ticks if t >= start]
        if not prices:
            return None
        opened = candles[-1]["close"]
        volumes = [c.get("volume", 0.0) for c in candles[-VOLUME_CANDLES:]]
        return {"start": start, "open": opened, "high": max(opened, *prices), "low": min(opened, *prices),
                "close": prices[-1], "volume": sum(volumes) / len(volumes) * min((now - start) / seconds, 1.0)}

    def indicators(self, name, profile, now):
        """Model `name`'s indicators on from where its backtest ended: {"time", "close", "values": {output:
        [...]}}, from its backtest's last candle (at its start, as the chart draws it), through each candle
        closed since, to the open candle as it stands at `now` (at `now`). None if they can't be computed."""
        from model_config import CompiledModel
        granularity, seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
        market = (profile["product_id"], granularity)
        try:
            model = self._compiled.get(name)
            if model is None:
                model = self._compiled[name] = CompiledModel(profile, seconds)
            candles = list(self.candles[market])
            forming = self._forming(market, now) if now < self.ends + seconds else None
            if forming is not None:
                candles.append(forming)
            series = model.evaluate(candles)[0]
        except Exception:  # the live indicators are a view; a failure mustn't stop the session
            return None
        since = self.markets[market]["history_last"]
        first = next((index for index, candle in enumerate(candles) if candle["start"] >= since), len(candles))
        times = [candle["start"] for candle in candles[first:]]
        if forming is not None:
            times[-1] = now
        if len(times) < 2:
            return None
        outputs = [output for meta in model.indicator_meta() for output in meta["outputs"]]
        return {"time": times, "close": [float(v) for v in series["close"][first:]],
                "values": {output: [float(v) for v in series[output][first:]] for output in outputs}}

    def snapshot(self, now, finished=False, stopped=False):
        """Everything the window shows, as plain data (also written as the session's record)."""
        now = min(now, self.ends) if finished and not stopped else now
        fraction = min(max((now - self.started) / self.horizon, 0.0), 1.0)
        price = self.ticks[-1][1]
        closest, distances = closest_direction(self.targets, self.started, self.anchor_price, self.horizon,
                                               self.ticks[1:])
        if self.odds is None:
            odds = [target.get("probability") for target in self.targets]
        else:
            odds = self.odds.at(min(now, self.ends) - self.started, math.log(price / self.anchor_price))
        likely = None if None in odds else int(np.argmax(odds))
        rows = []
        for name, profile in self.models:
            projection, live = self.projections.get(name), self.live.get(name)
            historic = self.historic.get(name)
            expected = None
            if projection is not None and closest is not None and live is not None:
                expected = expected_by(projection[closest]["curve"], live["curve"]["time"][-1])
            rows.append({"model": name, "historic_return": historic["total_return"] if historic else None,
                         "projected": projection, "live": live, "expected": expected,
                         "indicators": self.indicators(name, profile, now),
                         "versus": None if expected is None else (1 + live["total_return"]) / (1 + expected) - 1,
                         "note": self.notes.get(name) or ("its candles are longer than the session, so none close "
                                                          "during it" if projection is None else "")})
        return {"product_id": self.product_id, "started": self.started, "ends": self.ends, "now": now,
                "fraction": fraction, "finished": finished, "stopped": stopped, "anchor_price": self.anchor_price,
                "price": price, "change": price / self.anchor_price - 1, "targets": self.targets,
                "closest": closest, "distances": distances, "odds": odds, "likely": likely,
                "settings": self.settings,
                "expected_change": None if closest is None else projected_change(self.targets[closest], fraction),
                "lines": projection_lines(self.targets, self.started, self.anchor_price, self.horizon),
                "ticks": _downsample(self.ticks, 2000), "lead_in": self.lead_in, "rows": rows}


def monitor(client, sessions, stop, poll_seconds, on_update, now=time.time, cache_dir=DEFAULT_CACHE_DIR):
    """Watch the markets until every session ends or `stop` (a threading.Event) is set; True if all ran to the end.

    `sessions` are LiveSessions (one per currency), or one. Every `poll_seconds` each records its currency's
    live price; whenever a candle of a model's market has closed, it is fetched (into the candle store, like
    any download) and the market's models trade it. After each check, `on_update(snapshot)` is called for each
    session still running; a session that has ended is no longer checked.
    """
    running = [sessions] if isinstance(sessions, LiveSession) else list(sessions)
    while True:
        moment = now()
        for session in list(running):
            session.add_tick(moment, latest_price(client, session.product_id))
            for market in session.markets:
                if session.due(market, moment):
                    seconds = session.markets[market]["seconds"]
                    start, end = lookback_window(seconds,
                                                 now=datetime.fromtimestamp(moment - SETTLE_SECONDS, timezone.utc))
                    session.update_market(market, load_candles(client, *market, start, end, cache_dir=cache_dir))
            # A candle Coinbase never publishes (no trades in it) mustn't hold the session open past its settle time.
            complete = moment >= session.ends and (session.caught_up() or moment >= session.ends + SETTLE_SECONDS)
            on_update(session.snapshot(moment, finished=complete))
            if complete:
                running.remove(session)
        if not running:
            return True
        wait = min(session.ends + SETTLE_SECONDS - moment for session in running)
        if stop.wait(max(1.0, min(poll_seconds, wait))):
            return False


# --- describing a session -------------------------------------------------------------------

def _pct(value):
    return "-" if value is None else f"{value:+.2%}"


def describe(snapshot):
    """One line on where the session stands."""
    product, elapsed = snapshot["product_id"], format_duration(snapshot["now"] - snapshot["started"])
    period = format_duration(snapshot["ends"] - snapshot["started"])
    if snapshot["finished"]:
        head = f"Semi-live session finished: {product} moved {_pct(snapshot['change'])} over {period}"
    elif snapshot["stopped"]:
        head = f"Semi-live session stopped after {elapsed} of {period}: {product} moved {_pct(snapshot['change'])}"
    else:
        head = (f"Monitoring {product}: {elapsed} of {period} "
                f"({format_duration(snapshot['ends'] - snapshot['now'])} left), price {snapshot['price']:,.6g} "
                f"({_pct(snapshot['change'])})")
    if snapshot["closest"] is not None:
        target = snapshot["targets"][snapshot["closest"]]
        head += (f"; tracking closest to {target['name']} ({_pct(snapshot['expected_change'])} expected by now, "
                 f"{_pct(target['change'])} by the end)")
    if snapshot.get("likely") is not None:
        head += (f"; most likely now {snapshot['targets'][snapshot['likely']]['name']} "
                 f"({snapshot['odds'][snapshot['likely']]:.0%})")
    live = [row for row in snapshot["rows"] if row["live"] is not None]
    if live:
        best = max(live, key=lambda row: row["live"]["total_return"])
        head += f"; best model {best['model']} {_pct(best['live']['total_return'])}"
        if best["expected"] is not None:
            head += f" (expected {_pct(best['expected'])})"
    return head


def describe_all(snapshots):
    """One line on several sessions (one per currency): describe() for one, else each currency's move and most
    likely direction, with the best model across them."""
    snapshots = list(snapshots)
    if len(snapshots) == 1:
        return describe(snapshots[0])
    first = snapshots[0]
    period = format_duration(first["ends"] - first["started"])
    if all(s["finished"] for s in snapshots):
        head = f"Semi-live sessions finished ({period})"
    elif any(s["stopped"] for s in snapshots):
        head = f"Semi-live sessions stopped after {format_duration(first['now'] - first['started'])} of {period}"
    else:
        head = (f"Monitoring {len(snapshots)} currencies: {format_duration(first['now'] - first['started'])} of "
                f"{period} ({format_duration(first['ends'] - first['now'])} left)")
    parts = []
    for snapshot in snapshots:
        part = f"{snapshot['product_id']} {_pct(snapshot['change'])}"
        if snapshot.get("likely") is not None:
            part += (f" (most likely {snapshot['targets'][snapshot['likely']]['name']} "
                     f"{snapshot['odds'][snapshot['likely']]:.0%})")
        parts.append(part)
    live = [row for snapshot in snapshots for row in snapshot["rows"] if row["live"] is not None]
    tail = ""
    if live:
        best = max(live, key=lambda row: row["live"]["total_return"])
        tail = f"; best model {best['model']} {_pct(best['live']['total_return'])}"
    return f"{head}: " + ", ".join(parts) + tail


def write_session(snapshot, folder=None):
    """Write the session's record: session.json (everything) and prices.csv (every observed price). The folder."""
    stamp = datetime.fromtimestamp(snapshot["started"], timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(folder or SESSION_DIR / f"{snapshot['product_id']}_{stamp}")
    folder.mkdir(parents=True, exist_ok=True)
    record = dict(snapshot)
    (folder / "session.json").write_text(json.dumps(record, indent=1, default=float) + "\n", encoding="utf-8")
    with open(folder / "prices.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_utc", "price", "change",
                         *[f"{t['name']} ({t['change']:+.2%} by the end)" for t in snapshot["targets"]]])
        period = snapshot["ends"] - snapshot["started"]
        for moment, price in snapshot["ticks"]:
            fraction = (moment - snapshot["started"]) / period
            writer.writerow([datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                             f"{price:.8g}", f"{price / snapshot['anchor_price'] - 1:.6f}",
                             *[f"{snapshot['anchor_price'] * (1 + projected_change(t, fraction)):.8g}"
                               for t in snapshot["targets"]]])
    return folder


# --- the chart -------------------------------------------------------------------------------

def _dates(times):
    return np.array([int(t) for t in times], dtype="datetime64[s]")


def bridge(snapshot, last_time, last_close, candle_seconds=0):
    """(times, prices) joining a price chart's last point (`last_time`, `last_close`, of `candle_seconds` candles)
    to the session's first price, through the session's lead-in prices after that candle closed, so the history
    and the live price draw as one line."""
    points = [(last_time, last_close)]
    points += [(t, p) for t, p in snapshot.get("lead_in", ()) if last_time + candle_seconds < t < snapshot["started"]]
    if snapshot["ticks"] and snapshot["ticks"][0][0] > points[-1][0]:
        points.append(tuple(snapshot["ticks"][0]))
    return [t for t, _ in points], [p for _, p in points]


def draw_live(ax, snapshot, last_time=None, last_close=None, candle_seconds=0, history_style=None):
    """Draw the session onto a price chart's axes `ax` (the Chart tab's): the price on from the history's last
    point (`last_time`, `last_close`) to the session's start in `history_style` (the history line's own), the live
    price from there, and the five directions fanning out from where the session started, each labelled with its
    probability now; the most probable is bold. Returns (the artists, [(x, y)] date-number series of the lines,
    for fitting the price axis)."""
    from matplotlib.dates import date2num
    artists, series = [], []

    def line(times, prices, **style):
        x = date2num(_dates(times))
        artists.extend(ax.plot(x, prices, **style))
        series.append((np.asarray(x, dtype=float), np.asarray(prices, dtype=float)))
        return x

    ticks = snapshot["ticks"]
    if last_time is not None and last_close and ticks and last_time < ticks[0][0]:
        line(*bridge(snapshot, last_time, last_close, candle_seconds),
             **(history_style or {"color": "#1f2328", "linewidth": 1.2}), zorder=6)
    lines, odds = snapshot["lines"], snapshot.get("odds") or [None] * len(snapshot["targets"])
    x = date2num(_dates(lines["time"]))
    artists.append(ax.fill_between(x, lines["lines"][-1], lines["lines"][0], color="#8c959f", alpha=0.1,
                                   linewidth=0, zorder=1))
    for number, (target, prices) in enumerate(zip(snapshot["targets"], lines["lines"])):
        bold = number == snapshot.get("likely")
        line(lines["time"], prices, color=DIRECTION_COLORS[number], linestyle="--", linewidth=2.4 if bold else 1.2,
             zorder=7 if bold else 6)
        chance = "" if odds[number] is None else f"{odds[number]:.0%} "
        artists.append(ax.annotate(f"{chance}{target['name']} {target['change']:+.2%}", (x[-1], prices[-1]),
                                   xytext=(-4, 0), textcoords="offset points", ha="right", va="center",
                                   fontsize=8, color=DIRECTION_COLORS[number], fontweight="bold" if bold else None,
                                   clip_on=True, zorder=8,
                                   bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none",
                                             alpha=0.75)))
    line([t for t, _ in ticks], [p for _, p in ticks], color="#1f2328", linewidth=1.8, zorder=9)
    started = float(date2num(np.datetime64(int(snapshot["started"]), "s")))
    now = float(date2num(np.datetime64(int(min(snapshot["now"], snapshot["ends"])), "s")))
    artists.append(ax.axvline(started, color="#8c959f", linewidth=0.9, zorder=1))
    artists.append(ax.axvline(now, color="#0969da", linewidth=0.9, linestyle=":", zorder=1))
    artists.append(ax.annotate(f"Live {snapshot['price']:,.6g} ({_pct(snapshot['change'])})", (now, ticks[-1][1]),
                               xytext=(4, 10), textcoords="offset points", fontsize=8, color="#1f2328",
                               clip_on=True, zorder=9))
    return artists, series

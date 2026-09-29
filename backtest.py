"""Historical candle download and strategy backtesting for the simulator.

Candles come from the Advanced Trade API:
GET https://api.coinbase.com/api/v3/brokerage/products/{product_id}/candles
Docs: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-candles
"""

import bisect
import collections
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from friction import FrictionModel
from indicators import parse_period
from model_config import CompiledModel

LOOKBACK_YEARS = 10
DEFAULT_CACHE_DIR = Path(__file__).parent / "data"
CANDLE_FIELDS = ["start", "open", "high", "low", "close", "volume"]
MAX_CANDLES_PER_REQUEST = 350  # API limit
DEFAULT_STARTING_CAPITAL = 10_000.0
DEFAULT_DRAWDOWN_WINDOW = "180d"  # reported as the worst drawdown within any 6 months

GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 5 * 60,
    "FIFTEEN_MINUTE": 15 * 60,
    "THIRTY_MINUTE": 30 * 60,
    "ONE_HOUR": 60 * 60,
    "TWO_HOUR": 2 * 60 * 60,
    "SIX_HOUR": 6 * 60 * 60,
    "ONE_DAY": 24 * 60 * 60,
}
# Names used in the config documentation that differ from the API enum.
GRANULARITY_ALIASES = {"HOURLY": "ONE_HOUR", "DAILY": "ONE_DAY"}

# Profile settings the backtest reads but does not model yet, reported with each result.
UNMODELED_SETTINGS = [
    "friction_model.liquidity_buffer (superseded by liquidity_fee_buffer)",
    "friction_model.rebalancing_cost_adjustment (open positions are never rebalanced)",
    "capital_allocation.value_harvesting_rate",
    "tax_allocation",
    "safeguards.volatility_cap",
    "safeguards.risk_budget_per_trade",
]


def resolve_granularity(name):
    """Return the API granularity enum and its length in seconds."""
    granularity = GRANULARITY_ALIASES.get(str(name).upper(), str(name).upper())
    if granularity not in GRANULARITY_SECONDS:
        raise ValueError(
            f"Unsupported period_granularity '{name}'. "
            f"Use one of: {', '.join(GRANULARITY_SECONDS)}"
        )
    return granularity, GRANULARITY_SECONDS[granularity]


def lookback_window(granularity_seconds, now=None, years=LOOKBACK_YEARS):
    """Return [start, end) unix timestamps covering `years` of closed candles."""
    now = now or datetime.now(timezone.utc)
    try:
        start_time = now.replace(year=now.year - years)
    except ValueError:  # Feb 29 in a non-leap target year
        start_time = now.replace(year=now.year - years, day=28)
    # Align to the granularity so every window starts on a candle boundary.
    end = int(now.timestamp()) // granularity_seconds * granularity_seconds
    start = int(start_time.timestamp()) // granularity_seconds * granularity_seconds
    return start, end


def fetch_candles(client, product_id, granularity, start, end, progress=None, pause=0.1):
    """Fetch every candle in [start, end), paging in windows the API accepts.

    `progress(fraction, message)` is called after each page.
    """
    granularity, granularity_seconds = resolve_granularity(granularity)
    # One candle short of the limit, leaving room for the 1s start offset below.
    window = (MAX_CANDLES_PER_REQUEST - 1) * granularity_seconds
    candles = {}

    for window_start in range(start, end, window):
        window_end = min(window_start + window, end)
        # The API excludes a candle starting exactly at `start`, so ask from 1s earlier.
        response = client.get_candles(
            product_id=product_id,
            start=str(window_start - 1),
            end=str(window_end),
            granularity=granularity,
            limit=MAX_CANDLES_PER_REQUEST,
        )
        # Keyed by start time so candles on a window boundary aren't duplicated.
        # The API includes a candle starting at `end`; drop it so the still-open
        # current candle is never simulated.
        for candle in response.candles or []:
            candle_start = int(candle.start)
            if start <= candle_start < end:
                candles[candle_start] = {
                    "start": candle_start,
                    "open": float(candle.open),
                    "high": float(candle.high),
                    "low": float(candle.low),
                    "close": float(candle.close),
                    "volume": float(candle.volume),
                }

        if progress is not None:
            fetched_through = datetime.fromtimestamp(window_end, timezone.utc)
            progress(
                (window_end - start) / (end - start),
                f"{len(candles):,} candles fetched through {fetched_through:%Y-%m-%d}",
            )
        if pause:
            time.sleep(pause)  # stay well under the rate limit

    return [candles[t] for t in sorted(candles)]


def _cache_paths(cache_dir, product_id, granularity):
    stem = Path(cache_dir) / f"{product_id}_{granularity}"
    return stem.with_suffix(".csv"), stem.with_suffix(".meta.json")


def load_cached_candles(cache_dir, product_id, granularity):
    """Return (candles, fetched_start, fetched_end) from the cache, or None if absent."""
    csv_path, meta_path = _cache_paths(cache_dir, product_id, granularity)
    if not csv_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with csv_path.open(newline="", encoding="utf-8") as handle:
        candles = [
            {"start": int(row["start"]), **{field: float(row[field]) for field in CANDLE_FIELDS[1:]}}
            for row in csv.DictReader(handle)
        ]
    return candles, int(meta["fetched_start"]), int(meta["fetched_end"])


def save_cached_candles(cache_dir, product_id, granularity, candles, fetched_start, fetched_end):
    csv_path, meta_path = _cache_paths(cache_dir, product_id, granularity)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDLE_FIELDS)
        writer.writeheader()
        writer.writerows(candles)
    # The fetched range is kept separately because periods with no trades have no
    # candles; without it a product younger than the lookback would refetch every run.
    meta_path.write_text(
        json.dumps({"fetched_start": fetched_start, "fetched_end": fetched_end}, indent=2),
        encoding="utf-8",
    )


def load_candles(client, product_id, granularity, start, end, cache_dir=DEFAULT_CACHE_DIR, progress=None):
    """Return candles for [start, end), fetching only the range the cache doesn't cover."""
    granularity, _ = resolve_granularity(granularity)
    cached = load_cached_candles(cache_dir, product_id, granularity)
    if cached is None:
        candles, fetched_start, fetched_end = [], start, start
        missing = [(start, end)]
    else:
        candles, fetched_start, fetched_end = cached
        missing = [(s, e) for s, e in [(start, fetched_start), (max(start, fetched_end), end)] if s < e]

    new_count = 0
    if missing:
        total = sum(e - s for s, e in missing)
        done = 0
        merged = {candle["start"]: candle for candle in candles}
        for missing_start, missing_end in missing:
            span = missing_end - missing_start

            def report(fraction, message, done=done, span=span):
                if progress is not None:
                    progress((done + fraction * span) / total, message)

            for candle in fetch_candles(client, product_id, granularity, missing_start, missing_end, report):
                new_count += candle["start"] not in merged
                merged[candle["start"]] = candle
            done += span
        candles = [merged[t] for t in sorted(merged)]
        fetched_start, fetched_end = min(fetched_start, start), max(fetched_end, end)
        save_cached_candles(cache_dir, product_id, granularity, candles, fetched_start, fetched_end)

    # Counts describe the window being simulated, not everything in the cache.
    candles = [candle for candle in candles if start <= candle["start"] < end]
    if progress is not None:
        from_cache = len(candles) - new_count
        if not new_count:
            message = f"{len(candles):,} candles loaded from cache"
        elif not from_cache:
            message = f"{len(candles):,} candles downloaded"
        else:
            message = f"{len(candles):,} candles ({from_cache:,} from cache, {new_count:,} new)"
        progress(1.0, message)
    return candles


def _format_day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _year_of(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).year


def _start_of_year(year):
    return int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())


FLASH_CRASH_BRIEF_SECONDS = 3_600  # a crash that recovers within an hour is treated as bad data


def find_flash_crashes(candles, drop, granularity_seconds):
    """Find flash crashes and return (episodes, candles as the strategy should see them).

    A crash starts when a candle trades `drop` or more below the previous close, and it
    recovers at the first close back above that level. One that recovers within an hour
    is treated as a bad data point: in the returned candles its prices are replaced by the
    pre-crash close, so indicators, stops and valuation ignore it. A longer crash is left
    as it happened. The input list is not modified.
    """
    if not drop:
        return [], candles
    episodes, clean = [], list(candles)
    i = 1
    while i < len(candles):
        reference = clean[i - 1]["close"]
        threshold = reference * (1 - drop)
        if candles[i]["low"] > threshold:
            i += 1
            continue
        j = i
        while j < len(candles) and candles[j]["close"] <= threshold:
            j += 1
        recovered = j < len(candles)
        lasted = (candles[j]["start"] + granularity_seconds - candles[i]["start"]) if recovered else None
        brief = recovered and lasted <= FLASH_CRASH_BRIEF_SECONDS
        last = j if recovered else len(candles) - 1
        episodes.append({
            "start": i, "end": j if recovered else None, "reference": reference, "threshold": threshold,
            "low": min(candle["low"] for candle in candles[i:last + 1]), "brief": brief,
            "start_time": candles[i]["start"], "minutes": lasted / 60 if recovered else None,
        })
        if brief:
            for k in range(i, j + 1):
                fixed = dict(candles[k])
                for key in ("open", "close"):
                    if fixed[key] <= threshold:
                        fixed[key] = reference
                if fixed["low"] <= threshold:
                    fixed["low"] = min(fixed["open"], fixed["close"])
                fixed["high"] = max(fixed["high"], fixed["open"], fixed["close"])
                clean[k] = fixed
        i = j + 1 if recovered else len(candles)
    return episodes, clean


def flash_crash_levels_of(profile):
    """The (drop, allocation) levels of strategy.flash_crash_buy, deepest last; empty if off.

    Each level is a resting limit buy at `drop` below the previous candle's close. With a
    `reserve`, each is sized at `allocation` of that fixed amount; otherwise at `allocation`
    of the cash available when it fills.
    """
    settings = profile.get("strategy", {}).get("flash_crash_buy") or {}
    if not settings.get("enabled", True) or not settings:
        return []
    levels = settings.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ValueError("strategy.flash_crash_buy.levels must list at least one {\"drop\": ..., \"allocation\": ...}")
    parsed = []
    for level in levels:
        drop, allocation = (level.get("drop"), level.get("allocation")) if isinstance(level, dict) else (None, None)
        if not isinstance(drop, (int, float)) or isinstance(drop, bool) or not 0 < drop < 1:
            raise ValueError(f"strategy.flash_crash_buy level {level!r}: drop must be between 0 and 1, "
                             "such as 0.5 for 50% below the previous close")
        if not isinstance(allocation, (int, float)) or isinstance(allocation, bool) or not 0 < allocation <= 1:
            raise ValueError(f"strategy.flash_crash_buy level {level!r}: allocation must be above 0 and at most 1")
        parsed.append((float(drop), float(allocation)))
    if flash_crash_reserve_of(profile) and sum(allocation for _, allocation in parsed) > 1 + 1e-9:
        raise ValueError("strategy.flash_crash_buy level allocations are shares of the reserve and must add up "
                         "to at most 1")
    return sorted(parsed)


def flash_crash_reserve_of(profile):
    """The fixed cash amount set aside for flash-crash buys, or 0 when they share all cash."""
    settings = profile.get("strategy", {}).get("flash_crash_buy") or {}
    if not settings or not settings.get("enabled", True) or "reserve" not in settings:
        return 0.0
    reserve = settings["reserve"]
    if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or reserve <= 0:
        raise ValueError(f"strategy.flash_crash_buy.reserve must be a positive amount, not {reserve!r}")
    return float(reserve)


def starting_capital_of(profile):
    """The model's starting value: capital_allocation.starting_capital, or the default."""
    value = profile.get("capital_allocation", {}).get("starting_capital", DEFAULT_STARTING_CAPITAL)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"capital_allocation.starting_capital must be a positive number, not {value!r}")
    return float(value)


def run_backtest(candles, profile, starting_capital=None, progress=None, friction=None,
                 start_time=None, wait_for_fresh_signal=False):
    """Simulate a long-only strategy over `candles` using the profile's indicators and rules.

    The model starts with `starting_capital`, or the profile's
    capital_allocation.starting_capital when it isn't given.

    With `start_time` (a unix timestamp), the test begins at the first candle at or after
    it: the model holds its starting capital in cash and looks for an entry from there.
    Indicators still warm up on the history before it, so they are fully formed on the
    first day, while returns, buy and hold, drawdown, taxes and the charted series all
    start at `start_time`. With `wait_for_fresh_signal`, an entry signal already true at
    the start is not taken; the model waits for it to switch off and fire again.

    Entry and exit rules (see model_config.py) are evaluated on each candle's close and
    filled at that close. Hard stops and profit locks trigger intrabar on the candle's low/high and
    fill at their trigger price; when both are hit in one candle the stop is assumed
    first. Every entry signal must pass the friction screen (see friction.py), and
    tax on each calendar year's net realized gain is paid from cash when the next
    year begins, with losses carried forward. `progress(fraction, message)` is called
    about once per percent.
    """
    if not candles:
        raise ValueError("No candles to simulate")
    if starting_capital is None:
        starting_capital = starting_capital_of(profile)

    _, granularity_seconds = resolve_granularity(profile.get("period_granularity", "FIFTEEN_MINUTE"))
    model = CompiledModel(profile, granularity_seconds)
    profile = model.profile
    strategy = profile.get("strategy", {})
    exit_rules = strategy.get("exit_signal", {})
    friction = friction or FrictionModel(profile)
    safeguards = profile.get("safeguards", {})
    execution = profile.get("execution", {})

    if strategy.get("direction", "LONG_ONLY") != "LONG_ONLY":
        raise ValueError("Only LONG_ONLY strategies can be simulated so far")

    # Flash crashes that recover within an hour are removed from what the strategy sees;
    # longer ones stay. The raw candles still drive the flash-crash buys.
    flash_levels = flash_crash_levels_of(profile)
    crash_episodes, market_candles = find_flash_crashes(
        candles, flash_levels[0][0] if flash_levels else None, granularity_seconds)

    # Indicators and signals are computed on the full history, so the earlier candles
    # serve as warm-up; the test itself covers only the candles from the start time on.
    indicator_series, entry_signals, exit_signals = model.evaluate(market_candles)
    first = 0
    if start_time is not None:
        first = bisect.bisect_left([candle["start"] for candle in candles], start_time)
        if first >= len(candles):
            raise ValueError(f"The start date {_format_day(start_time)} is after the last candle "
                             f"({_format_day(candles[-1]['start'])})")
    candles = candles[first:]
    market_candles = market_candles[first:]
    # Which flash crash, if any, each candle of the test belongs to.
    episode_at = [None] * len(candles)
    for episode in crash_episodes:
        last = episode["end"] if episode["end"] is not None else first + len(candles) - 1
        for full_index in range(max(episode["start"], first), last + 1):
            episode_at[full_index - first] = episode
        episode["filled"] = set()
    entry_signals = entry_signals[first:].tolist()
    exit_signals = exit_signals[first:].tolist() if exit_signals is not None else None
    indicator_series = {name: values[first:] for name, values in indicator_series.items()}
    waiting_for_fresh_signal = wait_for_fresh_signal

    fee_rate = friction.fill_fee_rate
    slippage = friction.slippage
    position_fraction = float(profile.get("capital_allocation", {}).get("target_position_fraction", 1.0))
    max_orders_per_day = int(execution.get("max_orders_per_day", 0)) or None
    use_hard_stop = safeguards.get("use_hard_stop", False) and exit_rules.get("hard_stop_trigger", True)
    hard_stop_pct = float(safeguards.get("hard_stop_percentage", 0.0))
    profit_lock = float(exit_rules.get("profit_lock_target", 0.0))
    max_drawdown_limit = float(safeguards.get("max_drawdown_limit", 0.0))
    # With a drawdown window, the limit applies to the model's value against its highest
    # value in that trailing window, and is enforced with an equity stop (see below).
    window_limited = bool(max_drawdown_limit and safeguards.get("drawdown_window"))
    window_setting = safeguards.get("drawdown_window") or DEFAULT_DRAWDOWN_WINDOW
    window_candles = parse_period(window_setting, granularity_seconds, "safeguards.drawdown_window")
    round_trip_cost = 2 * (fee_rate + slippage) * position_fraction
    # Cash set aside for flash-crash buys; the main strategy never trades it.
    flash_reserve = flash_crash_reserve_of(profile) if flash_levels else 0.0
    trailing_stop = float(exit_rules.get("trailing_stop", 0.0) or 0.0)
    if not 0 <= trailing_stop < 1:
        raise ValueError(f"strategy.exit_signal.trailing_stop must be between 0 and 1, not {trailing_stop!r}")
    # A resting limit order adds liquidity, so it pays the maker fee rather than the taker fee.
    maker_fee_rate = friction.maker_rate * friction.fee_buffer_multiplier

    cash = float(starting_capital)
    units = 0.0
    entry = None
    flash_lots = []  # flash-crash buys held until the crash recovers
    trades = []
    fees_paid = 0.0
    peak_equity = cash  # all-time peak, for the reported max drawdown
    limit_peak_equity = cash  # peak the drawdown limit measures from; reset on resume
    max_drawdown = 0.0
    drawdown_pauses = []
    paused = False
    trend_reset = False
    orders_today = 0
    current_day = None
    screened_signals = 0
    signal_blocked = False
    tax_year = _year_of(candles[0]["start"])
    next_tax_settlement = _start_of_year(tax_year + 1)
    realized_by_year = {}
    loss_carryforward = 0.0
    tax_payments = []
    # Highest value in the trailing drawdown window, as a monotonic queue of (index, value).
    window_peaks = collections.deque()
    max_window_drawdown = 0.0

    def window_peak(index):
        while window_peaks and window_peaks[0][0] <= index - window_candles:
            window_peaks.popleft()
        return window_peaks[0][1] if window_peaks else cash

    def record_equity(index, value):
        while window_peaks and window_peaks[-1][1] <= value:
            window_peaks.pop()
        window_peaks.append((index, value))

    def settle_taxes(year, price):
        """Pay tax on the year's net realized gain after applying carried-forward losses.

        A tax payment is a withdrawal, not a trading loss, so drawdown peaks shrink in
        proportion rather than counting the payment as a drawdown.
        """
        nonlocal cash, loss_carryforward, peak_equity, limit_peak_equity
        net_gain = realized_by_year.get(year, 0.0)
        taxable = net_gain - loss_carryforward
        tax = max(taxable, 0.0) * friction.tax_rate
        loss_carryforward = max(-taxable, 0.0)
        equity_before = cash + (units + sum(lot["units"] for lot in flash_lots)) * price
        cash -= tax
        if tax and equity_before > 0:
            kept = (equity_before - tax) / equity_before
            peak_equity *= kept
            limit_peak_equity *= kept
            for position, (peak_index, value) in enumerate(window_peaks):
                window_peaks[position] = (peak_index, value * kept)
        tax_payments.append({
            "year": year, "net_realized_gain": net_gain, "taxable_gain": max(taxable, 0.0),
            "tax": tax, "loss_carryforward": loss_carryforward,
        })

    def buy(candle, fill_price, reason):
        nonlocal cash, units, entry, fees_paid, orders_today
        fill_price *= 1 + slippage
        spend = max(cash - flash_reserve, 0.0) * position_fraction
        fee = spend * fee_rate
        units = (spend - fee) / fill_price
        cash -= spend
        fees_paid += fee
        orders_today += 1
        entry = {"time": candle["start"], "price": fill_price, "cost": spend, "fee": fee, "reason": reason,
                 "highest": candle["close"]}

    def sell(candle, fill_price, reason):
        nonlocal cash, units, entry, fees_paid, orders_today
        fill_price *= 1 - slippage
        proceeds = units * fill_price
        fee = proceeds * fee_rate
        cash += proceeds - fee
        fees_paid += fee
        orders_today += 1
        pnl = proceeds - fee - entry["cost"]
        exit_year = _year_of(candle["start"])
        realized_by_year[exit_year] = realized_by_year.get(exit_year, 0.0) + pnl
        trades.append({
            "entry_time": entry["time"],
            "exit_time": candle["start"],
            "entry_price": entry["price"],
            "exit_price": fill_price,
            "position_cost": entry["cost"],
            "pnl": pnl,
            "fees": entry["fee"] + fee,
            "return": (proceeds - fee) / entry["cost"] - 1,
            "entry_reason": entry["reason"],
            "exit_reason": reason,
            "equity_after": cash,  # the position is closed, so equity is all cash
        })
        units = 0.0
        entry = None

    def buy_the_crash(candle, episode):
        """Fill any flash-crash limit buys this candle traded down to, holding them until recovery."""
        nonlocal cash, fees_paid
        for drop, allocation in flash_levels:
            limit_price = episode["reference"] * (1 - drop)
            if candle["low"] > limit_price or cash <= 0:
                break  # levels are sorted shallowest first, so deeper ones didn't fill either
            if drop in episode["filled"]:
                continue
            episode["filled"].add(drop)
            spend = min(flash_reserve * allocation, cash) if flash_reserve else cash * allocation
            fee = spend * maker_fee_rate
            cash -= spend
            fees_paid += fee
            flash_lots.append({"episode": episode, "drop": drop, "time": candle["start"], "price": limit_price,
                               "cost": spend, "fee": fee, "units": (spend - fee) / limit_price})

    def sell_the_rebound(candle, episode, mark_price):
        """Sell the lots bought in a flash crash at the close of the candle it recovered in."""
        nonlocal cash, fees_paid
        for lot in [lot for lot in flash_lots if lot["episode"] is episode]:
            flash_lots.remove(lot)
            exit_price = candle["close"] * (1 - slippage)
            proceeds = lot["units"] * exit_price
            fee = proceeds * fee_rate
            cash += proceeds - fee
            fees_paid += fee
            pnl = proceeds - fee - lot["cost"]
            year = _year_of(candle["start"])
            realized_by_year[year] = realized_by_year.get(year, 0.0) + pnl
            trades.append({
                "entry_time": lot["time"], "exit_time": candle["start"],
                "entry_price": lot["price"], "exit_price": exit_price,
                "position_cost": lot["cost"], "pnl": pnl, "fees": lot["fee"] + fee,
                "return": (proceeds - fee) / lot["cost"] - 1,
                "entry_reason": f"FLASH_CRASH_BUY_{lot['drop']:.0%}", "exit_reason": "FLASH_REBOUND_SELL",
                "equity_after": cash + (units + sum(held["units"] for held in flash_lots)) * mark_price,
            })

    total = len(candles)
    report_every = max(1, total // 100)
    equity_series = []

    for index, raw_candle in enumerate(candles):
        entry_signal = entry_signals[index]
        # The strategy trades on the market as it should be seen: a brief flash crash is ignored.
        candle = market_candles[index]
        episode = episode_at[index]

        day = candle["start"] // 86_400
        if day != current_day:
            current_day, orders_today = day, 0
        while candle["start"] >= next_tax_settlement:
            settle_taxes(tax_year, candle["open"])
            tax_year += 1
            next_tax_settlement = _start_of_year(tax_year + 1)
        if episode is not None:
            buy_the_crash(raw_candle, episode)
        # During a crash that lasts over an hour, keep holding: nothing is sold until it recovers.
        holding_through_crash = episode is not None and not episode["brief"] and index != (
            episode["end"] - first if episode["end"] is not None else None)

        if entry is not None and not holding_through_crash:
            stops = []
            if use_hard_stop and hard_stop_pct:
                stops.append((entry["price"] * (1 - hard_stop_pct), "HARD_STOP"))
            if trailing_stop:
                # Trails the highest price since entry, as of the previous candle, since a
                # candle's high and low can't be ordered within it.
                stops.append((entry["highest"] * (1 - trailing_stop), "TRAILING_STOP"))
            if window_limited:
                # The price at which selling (after its slippage and fee) would leave the
                # model exactly at the drawdown limit below its trailing-window peak.
                floor = window_peak(index) * (1 - max_drawdown_limit)
                stops.append(((floor - cash) / (units * (1 - slippage) * (1 - fee_rate)), "DRAWDOWN_STOP"))
            target_price = entry["price"] * (1 + profit_lock)
            triggered = [stop for stop in stops if candle["low"] <= stop[0]]
            if triggered:
                # The highest stop is reached first on the way down; a gap below it fills at the open.
                stop_price, reason = max(triggered)
                sell(candle, min(stop_price, candle["open"]), reason)
                if reason == "DRAWDOWN_STOP":
                    paused, trend_reset = True, False
                    drawdown_pauses.append({"paused_at": candle["start"], "resumed_at": None})
            elif profit_lock and candle["high"] >= target_price:
                sell(candle, max(target_price, candle["open"]), "PROFIT_LOCK")
            elif exit_signals is not None and exit_signals[index]:
                sell(candle, candle["close"], "EXIT_SIGNAL")
        if entry is not None:
            entry["highest"] = max(entry["highest"], candle["high"])
        if episode is not None and episode["end"] is not None and index == episode["end"] - first:
            sell_the_rebound(raw_candle, episode, candle["close"])

        equity = cash + (units + sum(lot["units"] for lot in flash_lots)) * candle["close"]
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak_equity)
        limit_peak_equity = max(limit_peak_equity, equity)
        record_equity(index, equity)
        max_window_drawdown = max(max_window_drawdown, 1 - equity / window_peak(index))
        if (not window_limited and not paused and max_drawdown_limit
                and 1 - equity / limit_peak_equity >= max_drawdown_limit):
            # Pause until the next prime opportunity: the entry signal has to switch off
            # (the trend that failed has ended) and then fire afresh before trading resumes.
            paused, trend_reset = True, False
            drawdown_pauses.append({"paused_at": candle["start"], "resumed_at": None})
            if entry is not None:
                sell(candle, candle["close"], "DRAWDOWN_LIMIT")
        if paused and not entry_signal:
            trend_reset = True
        if waiting_for_fresh_signal and not entry_signal:
            waiting_for_fresh_signal = False  # the signal has switched off; the next one is fresh

        order_room = max_orders_per_day is None or orders_today < max_orders_per_day
        can_enter = (not paused or trend_reset) and not waiting_for_fresh_signal
        if entry is None and can_enter and order_room:
            position_value = max(cash - flash_reserve, 0.0) * position_fraction
            # Under a windowed limit, enter only with room for two round trips' costs above
            # the floor, so the entry's own costs can't trigger the equity stop at once.
            no_room = window_limited and equity * (1 - 2 * round_trip_cost) < window_peak(index) * (
                1 - max_drawdown_limit)
            if not entry_signal or position_value <= 0 or no_room:
                signal_blocked = False
            elif not friction.passes_screen(candle["close"], position_value):
                # Count each blocked signal once, not every candle it stays true.
                screened_signals += not signal_blocked
                signal_blocked = True
            else:
                signal_blocked = False
                if paused:
                    paused = False
                    drawdown_pauses[-1]["resumed_at"] = candle["start"]
                    # Measure the limit from here so the old peak can't re-trigger it at once.
                    limit_peak_equity = equity
                buy(candle, candle["close"], "ENTRY_SIGNAL")

        equity_series.append(cash + (units + sum(lot["units"] for lot in flash_lots)) * candle["close"])

        if progress is not None and (index % report_every == 0 or index == total - 1):
            progress((index + 1) / total, f"{index + 1:,} / {total:,} candles, {len(trades):,} trades")

    last = market_candles[-1]
    ending_equity = cash + (units + sum(lot["units"] for lot in flash_lots)) * last["close"]
    wins = [trade for trade in trades if trade["pnl"] > 0]
    # The final, unfinished year's tax isn't due yet; estimate it for the report.
    open_year_taxable = realized_by_year.get(tax_year, 0.0) - loss_carryforward
    estimated_tax_due = max(open_year_taxable, 0.0) * friction.tax_rate
    return {
        "product_id": profile.get("product_id"),
        "requested_start": start_time,
        "waited_for_fresh_signal": wait_for_fresh_signal,
        "warmup_candles": first,
        # The strategy's first signal entry; flash-crash fills are opportunistic and reported separately.
        "first_entry": next((trade["entry_time"] for trade in trades if trade["entry_reason"] == "ENTRY_SIGNAL"),
                            entry["time"] if entry else None),
        "period_start": candles[0]["start"],
        "period_end": last["start"],
        "candle_count": total,
        "starting_capital": float(starting_capital),
        "ending_equity": ending_equity,
        "total_return": ending_equity / starting_capital - 1,
        "buy_and_hold_return": last["close"] / candles[0]["close"] - 1,
        "max_drawdown": max_drawdown,
        "max_window_drawdown": max_window_drawdown,
        "drawdown_window": window_setting,
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "fees_paid": fees_paid,
        "commission_rate": fee_rate,
        "taxes_paid": sum(payment["tax"] for payment in tax_payments),
        "tax_payments": tax_payments,
        "estimated_tax_due": estimated_tax_due,
        "screened_signals": screened_signals,
        "flash_crash_buys": [trade for trade in trades if trade["entry_reason"].startswith("FLASH_CRASH_BUY")],
        "flash_crash_events": [
            {key: episode[key] for key in ("start_time", "reference", "low", "brief", "minutes")}
            for episode in crash_episodes if episode["end"] is None or episode["end"] >= first],
        "open_flash_lots": len(flash_lots),
        "friction": friction.describe(candles[0]["close"], starting_capital * position_fraction),
        "open_position": entry is not None,
        "drawdown_pauses": drawdown_pauses,
        "trades": trades,
        "indicators": model.indicator_meta(),
        "rules": model.rule_text(),
        "series": {
            "time": [candle["start"] for candle in candles],
            "close": indicator_series["close"],
            "equity": equity_series,
            "indicators": {name: indicator_series[name] for meta in model.indicator_meta()
                           for name in meta["outputs"]},
        },
        "unmodeled_settings": UNMODELED_SETTINGS,
    }

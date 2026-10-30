"""Historical candle download and strategy backtesting for the simulator.

Candles come from the Advanced Trade API:
GET https://api.coinbase.com/api/v3/brokerage/products/{product_id}/candles
Docs: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-candles
"""

import bisect
import collections
import time
from datetime import datetime, timezone
from pathlib import Path

from candle_store import CandleStore, MarketCandles, MarketMetrics
from friction import FrictionModel
from indicators import parse_period
from model_config import CompiledModel
from taxes import LONG_TERM_SECONDS

LOOKBACK_YEARS = 10
DEFAULT_CACHE_DIR = Path(__file__).parent / "data"
MAX_CANDLES_PER_REQUEST = 350  # API limit
DEFAULT_STARTING_CAPITAL = 10_000.0
DEFAULT_DRAWDOWN_WINDOW = "180d"  # reported as the worst drawdown within any 6 months
# Bumped whenever a change alters backtest results, so caches of old results (the model
# search's registry) are never mixed with new ones. 3: per-trade tax reserves paid monthly.
# 4: losses offset gains within a tax year, with net losses carried forward (see taxes.py).
# 5: progressive federal and California tax on gains stacked on a salary, by default.
BACKTEST_VERSION = 5

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


def _market_candles(store, product_id, granularity, candles):
    return MarketCandles(candles, MarketMetrics(store, store.table_name(product_id, granularity)))


def load_cached_candles(cache_dir, product_id, granularity, read_only=False):
    """Return (candles, fetched_start, fetched_end) from the cache, or None if absent.

    The candles carry their market's metric cache, as load_candles describes. With
    `read_only`, nothing is written to the database (see CandleStore).
    """
    store = CandleStore(cache_dir, read_only=read_only)
    cached = store.load(product_id, granularity)
    if cached is None:
        return None
    candles, fetched_start, fetched_end = cached
    return _market_candles(store, product_id, granularity, candles), fetched_start, fetched_end


def load_candles(client, product_id, granularity, start, end, cache_dir=DEFAULT_CACHE_DIR, progress=None):
    """Return candles for [start, end), fetching only the range the cache doesn't cover.

    The returned list carries `metrics`, which lets every model backtested on these candles
    share its indicator values and keep slow ones in the market's table (see candle_store.py).
    """
    granularity, _ = resolve_granularity(granularity)
    store = CandleStore(cache_dir)
    cached = store.load(product_id, granularity)
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
        fetched = []
        for missing_start, missing_end in missing:
            span = missing_end - missing_start

            def report(fraction, message, done=done, span=span):
                if progress is not None:
                    progress((done + fraction * span) / total, message)

            for candle in fetch_candles(client, product_id, granularity, missing_start, missing_end, report):
                new_count += candle["start"] not in merged
                merged[candle["start"]] = candle
                fetched.append(candle)
            done += span
        candles = [merged[t] for t in sorted(merged)]
        fetched_start, fetched_end = min(fetched_start, start), max(fetched_end, end)
        store.save(product_id, granularity, fetched, fetched_start, fetched_end)

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
    return _market_candles(store, product_id, granularity, candles)


def _fall(value, peak):
    """The fall from `peak` to `value` as a fraction; a model left with nothing has fallen all the way."""
    return 1 - value / peak if peak > 0 else 1.0


def _format_day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def find_flash_crashes(candles, drop, granularity_seconds):
    """Find the flash crashes in `candles`.

    A crash starts when a candle trades `drop` or more below the previous close, and it
    recovers at the first close back above that level. Crashes are only located here: the
    candles are left as they traded, so the strategy sees every crash's real prices.

    Returns a dict per crash, where `start` and `end` index into `candles` (`end` is the
    recovery candle, or None if the data ends before it recovers) and `minutes` is how
    long it lasted.
    """
    if not drop:
        return []
    episodes = []
    i = 1
    while i < len(candles):
        reference = candles[i - 1]["close"]
        threshold = reference * (1 - drop)
        if candles[i]["low"] > threshold:
            i += 1
            continue
        j = i
        while j < len(candles) and candles[j]["close"] <= threshold:
            j += 1
        recovered = j < len(candles)
        lasted = (candles[j]["start"] + granularity_seconds - candles[i]["start"]) if recovered else None
        last = j if recovered else len(candles) - 1
        episodes.append({
            "start": i, "end": j if recovered else None, "reference": reference, "threshold": threshold,
            "low": min(candle["low"] for candle in candles[i:last + 1]),
            "start_time": candles[i]["start"], "minutes": lasted / 60 if recovered else None,
        })
        i = j + 1 if recovered else len(candles)
    return episodes


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
                 start_time=None, wait_for_fresh_signal=False, compute_device="cpu", device_id=0):
    """Simulate a long-only strategy over `candles` using the profile's indicators and rules.

    `candles` is the full history, oldest first (ideally from load_candles, whose metric
    cache other models on the same market share). `profile` is the model's config.
    `starting_capital` defaults to the profile's capital_allocation.starting_capital, and
    `friction` to a FrictionModel built from the profile. `progress(fraction, message)` is
    called about once per percent.

    With `start_time` (a unix timestamp), the test begins at the first candle at or after
    it: the model holds its starting capital in cash and looks for an entry from there.
    Indicators still warm up on the history before it, so they are fully formed on the
    first day, while returns, drawdown, taxes and the charted series all start at
    `start_time`. With `wait_for_fresh_signal`, an entry signal already true at the start
    is not taken; the model waits for it to switch off and fire again.

    Entry and exit rules (see model_config.py) are evaluated on each candle's close and
    filled at that close. Hard stops, trailing stops and profit locks trigger intrabar on
    the candle's low/high and fill at their trigger price (or the open, if it gapped past
    it); when a stop and the profit lock are both hit in one candle the stop is assumed
    first. Every entry signal must pass the friction screen (see friction.py). Tax on realized
    gains is set aside from trading cash as trades close and paid later (see taxes.py): by
    default losses offset gains within a tax year and net losses carry forward, paid yearly;
    with friction_model.tax_loss_offset false, each winning trade is taxed on its own and paid
    monthly. Flash crashes are traded as they happened: optional
    limit buys at strategy.flash_crash_buy levels, sold on the recovery candle's close;
    the model never sells into a crash (see the comment in the candle loop).

    Returns a dict; the main keys:
        period_start, period_end, candle_count   the tested candles (after `start_time`)
        warmup_candles     candles before the test start, used only for indicator warm-up
        starting_capital, ending_equity, total_return
        first_entry        time of the first signal entry, or None
        ready_to_trade     first candle on which every entry rule's indicators and
                           lookbacks have warmed up, or None if they never do
        buy_and_hold_start, buy_and_hold_return
                           buy and hold from the close of the ready_to_trade candle (or
                           the first entry, if that came sooner), so the warm-up doesn't
                           count against the model but its choice to wait in cash does
        buy_and_hold_from_test_start   buy and hold from the first tested candle
        max_drawdown, max_window_drawdown, drawdown_window
                           the worst drawdown overall and within any `drawdown_window`
        trade_count, win_rate, trades   closed trades, flash-crash lots included
        fees_paid, commission_rate, taxes_paid, taxes_reserved, tax_payments, estimated_tax_due
        screened_signals   entry signals the friction screen blocked
        flash_crash_buys, flash_crash_events, open_flash_lots
        open_position, drawdown_pauses, friction, indicators, rules, unmodeled_settings
        series             per-candle time, close, equity, tax_reserve and indicator values for charts
    """
    if not candles:
        raise ValueError("No candles to simulate")
    if compute_device not in ("cpu", "cuda"):
        raise ValueError(f"Unsupported compute_device {compute_device!r}; use 'cpu' or 'cuda'")
    if compute_device == "cuda":
        from gpu_indicators import require_devices

        devices = require_devices()
        if device_id not in devices:
            raise ValueError(f"CUDA device {device_id} is not available; visible devices are {devices}")
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

    flash_levels = flash_crash_levels_of(profile)
    crash_episodes = find_flash_crashes(candles, flash_levels[0][0] if flash_levels else None, granularity_seconds)

    # Indicators and signals are computed on the full history, so the earlier candles
    # serve as warm-up; the test itself covers only the candles from the start time on.
    # Candles from load_candles carry a metric cache that every model on them shares.
    metrics = getattr(candles, "metrics", None)
    indicator_series, entry_signals, exit_signals = model.evaluate(
        metrics.cache(candles) if metrics is not None else candles,
        compute_device=compute_device, device_id=device_id)
    entry_ready = model.entry_ready(indicator_series)
    regimes = model.regimes
    active_regime = None
    if regimes is not None:
        # Each candle trades on its active regime's signals (detected with lag; see regimes.py).
        active_regime, entry_signals, regime_exits, entry_ready = regimes.signals(
            indicator_series, len(entry_signals))
        exit_signals = regime_exits if any(block["all"] or block["any"] for block in regimes.exits) else None
    first = 0
    if start_time is not None:
        first = bisect.bisect_left([candle["start"] for candle in candles], start_time)
        if first >= len(candles):
            raise ValueError(f"The start date {_format_day(start_time)} is after the last candle "
                             f"({_format_day(candles[-1]['start'])})")
    candles = candles[first:]
    # Which flash crash, if any, each candle of the test belongs to.
    episode_at = [None] * len(candles)
    for episode in crash_episodes:
        last = episode["end"] if episode["end"] is not None else first + len(candles) - 1
        for full_index in range(max(episode["start"], first), last + 1):
            episode_at[full_index - first] = episode
        episode["filled"] = set()
    entry_signals = entry_signals[first:].tolist()
    entry_ready = entry_ready[first:]
    exit_signals = exit_signals[first:].tolist() if exit_signals is not None else None
    indicator_series = {name: values[first:] for name, values in indicator_series.items()}
    if active_regime is not None:
        active_regime = active_regime[first:].tolist()
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
    # The loss halt stops the model for good once its value falls below a share of its starting capital
    # or far enough below its peak: it recognises a market it is failing in and stops losing money there.
    loss_halt = safeguards.get("loss_halt") or {}
    halt_below = float(loss_halt.get("below_start", 0.0) or 0.0)
    halt_from_peak = float(loss_halt.get("from_peak", 0.0) or 0.0)
    for name, value in (("below_start", halt_below), ("from_peak", halt_from_peak)):
        if not 0 <= value < 1:
            raise ValueError(f"safeguards.loss_halt.{name} must be from 0 up to (not including) 1, not {value!r}")
    window_setting = safeguards.get("drawdown_window") or DEFAULT_DRAWDOWN_WINDOW
    window_candles = parse_period(window_setting, granularity_seconds, "safeguards.drawdown_window")
    # With a cooldown, trading resumes this long after the drawdown limit paused it, measuring the limit afresh
    # from there; without one, it waits for a fresh entry signal (or, with a window, room under the window's peak).
    cooldown_setting = safeguards.get("drawdown_cooldown")
    cooldown_candles = (parse_period(cooldown_setting, granularity_seconds, "safeguards.drawdown_cooldown")
                        if cooldown_setting else None)
    cooldown_ends = None  # the candle index the current pause's cooldown ends at
    round_trip_cost = 2 * (fee_rate + slippage) * position_fraction
    # Cash set aside for flash-crash buys; the main strategy never trades it.
    flash_reserve = flash_crash_reserve_of(profile) if flash_levels else 0.0
    trailing_stop = float(exit_rules.get("trailing_stop", 0.0) or 0.0)
    if not 0 <= trailing_stop < 1:
        raise ValueError(f"strategy.exit_signal.trailing_stop must be between 0 and 1, not {trailing_stop!r}")
    # With profit_lock_trail, reaching the profit target arms a trailing stop instead of selling.
    target_trail = float(exit_rules.get("profit_lock_trail", 0.0) or 0.0)
    if not 0 <= target_trail < 1:
        raise ValueError(f"strategy.exit_signal.profit_lock_trail must be between 0 and 1, not {target_trail!r}")
    if target_trail and not profit_lock:
        raise ValueError("strategy.exit_signal.profit_lock_trail needs a profit_lock_target to arm it")
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
    halted_at = None  # when the loss halt stopped the model, if it did
    orders_today = 0
    current_day = None
    screened_signals = 0
    signal_blocked = False
    # Tax on realized gains: what each closed trade sets aside or releases, and when it is paid (taxes.py).
    ledger = friction.tax_ledger()
    ledger.start(candles[0]["start"])
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

    def set_aside(tax, candle):
        """Move `tax` from trading cash into the tax reserve (negative: back from the reserve to trading cash)."""
        nonlocal cash, peak_equity, limit_peak_equity
        if not tax:
            return
        equity_before = cash + (units + sum(lot["units"] for lot in flash_lots)) * candle["close"]
        cash -= tax
        if equity_before > 0 and equity_before - tax > 0:
            # Tax moving in or out of the reserve isn't a market gain or loss: the drawdown peaks move with it.
            kept = (equity_before - tax) / equity_before
            peak_equity *= kept
            limit_peak_equity *= kept
            for position, (peak_index, value) in enumerate(window_peaks):
                window_peaks[position] = (peak_index, value * kept)

    def record_realized(pnl, candle, opened):
        """Set aside a closed trade's tax: its full tax on a gain, and on a loss, release the tax the loss saves
        (see taxes.py); `opened` is when it was bought, for long- or short-term rates. Returns the amount set
        aside (negative: released)."""
        tax = ledger.realize(pnl, candle["start"], long_term=candle["start"] - opened > LONG_TERM_SECONDS)
        set_aside(tax, candle)
        return tax

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
        holding = entry
        units, entry = 0.0, None
        tax = record_realized(pnl, candle, holding["time"])
        trades.append({
            "entry_time": holding["time"],
            "exit_time": candle["start"],
            "entry_price": holding["price"],
            "exit_price": fill_price,
            "position_cost": holding["cost"],
            "pnl": pnl,
            "tax_withheld": tax,
            "net_pnl": pnl - tax,
            "fees": holding["fee"] + fee,
            "return": (proceeds - fee - tax) / holding["cost"] - 1,
            "entry_reason": holding["reason"],
            "exit_reason": reason,
            "equity_after": cash,  # the position is closed; excludes any flash-crash lots still held
        })

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

    def sell_the_rebound(candle, episode, reason="FLASH_REBOUND_SELL"):
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
            tax = record_realized(pnl, candle, lot["time"])
            trades.append({
                "entry_time": lot["time"], "exit_time": candle["start"],
                "entry_price": lot["price"], "exit_price": exit_price,
                "position_cost": lot["cost"], "pnl": pnl, "tax_withheld": tax, "net_pnl": pnl - tax,
                "fees": lot["fee"] + fee, "return": (proceeds - fee - tax) / lot["cost"] - 1,
                "entry_reason": f"FLASH_CRASH_BUY_{lot['drop']:.0%}", "exit_reason": reason,
                "equity_after": cash + (units + sum(held["units"] for held in flash_lots)) * candle["close"],
            })

    def halt_floor():
        """The value below which the loss halt stops the model (0 when it is off)."""
        return max(float(starting_capital) * halt_below if halt_below else 0.0,
                   peak_equity * (1 - halt_from_peak) if halt_from_peak else 0.0)

    def exit_stops(index):
        """The (price, reason) stops guarding the open position on candle `index`; the live trader reads the
        next candle's from result["live_state"], so it checks the same levels the backtest does."""
        stops = []
        if use_hard_stop and hard_stop_pct:
            stops.append((entry["price"] * (1 - hard_stop_pct), "HARD_STOP"))
        if trailing_stop:
            # Trails the highest price since entry, as of the previous candle, since a
            # candle's high and low can't be ordered within it.
            stops.append((entry["highest"] * (1 - trailing_stop), "TRAILING_STOP"))
        if entry.get("target_armed"):
            stops.append((entry["highest"] * (1 - target_trail), "TARGET_TRAIL"))
        if window_limited:
            # The price at which selling (after its slippage and fee) would leave the
            # model exactly at the drawdown limit below its trailing-window peak.
            floor = window_peak(index) * (1 - max_drawdown_limit)
            stops.append(((floor - cash) / (units * (1 - slippage) * (1 - fee_rate)), "DRAWDOWN_STOP"))
        if halt_floor():
            # The price at which selling would leave the model's value exactly at the loss halt's floor.
            stops.append(((halt_floor() - cash) / (units * (1 - slippage) * (1 - fee_rate)), "LOSS_HALT"))
        return stops

    total = len(candles)
    report_every = max(1, total // 100)
    equity_series = []
    tax_reserve_series = []

    regime_switches = []
    for index, candle in enumerate(candles):
        entry_signal = entry_signals[index]
        episode = episode_at[index]
        if active_regime is not None:
            # The active regime's own stops, target and position size.
            regime = regimes.settings[active_regime[index]]
            use_hard_stop = regime["use_hard_stop"] and regime["hard_stop_trigger"]
            hard_stop_pct = regime["hard_stop_percentage"]
            profit_lock = regime["profit_lock_target"]
            target_trail = regime["profit_lock_trail"]
            trailing_stop = regime["trailing_stop"]
            position_fraction = regime["target_position_fraction"]
            round_trip_cost = 2 * (fee_rate + slippage) * position_fraction
            if index and active_regime[index] != active_regime[index - 1]:
                regime_switches.append({"time": candle["start"], "from": regimes.names[active_regime[index - 1]],
                                        "to": regimes.names[active_regime[index]]})

        day = candle["start"] // 86_400
        if day != current_day:
            current_day, orders_today = day, 0
        for payment in ledger.settle(candle["start"]):
            # The year's tax is paid from the reserve; what was set aside beyond it returns to trading cash.
            set_aside(-payment["refund"], candle)
            tax_payments.append(payment)
        if episode is not None and halted_at is None:
            buy_the_crash(candle, episode)
        # The model never sells into a flash crash. No stop or profit target fires on a crash
        # candle, the one it recovers in included, since that candle traded in the crash; exit
        # rules and the drawdown limit resume at the recovery candle's close.
        recovered_now = episode is not None and episode["end"] is not None and index == episode["end"] - first
        holding_through_crash = episode is not None and not recovered_now

        if entry is not None and episode is None:
            stops = exit_stops(index)
            target_price = entry["price"] * (1 + profit_lock)
            triggered = [stop for stop in stops if candle["low"] <= stop[0]]
            if triggered:
                # The highest stop is reached first on the way down; a gap below it fills at the open.
                stop_price, reason = max(triggered)
                sell(candle, min(stop_price, candle["open"]), reason)
                if reason == "DRAWDOWN_STOP":
                    paused, trend_reset = True, False
                    drawdown_pauses.append({"paused_at": candle["start"], "resumed_at": None})
                    cooldown_ends = index + cooldown_candles if cooldown_candles else None
                if reason == "LOSS_HALT":
                    halted_at = candle["start"]
            elif profit_lock and candle["high"] >= target_price and not entry.get("target_armed"):
                if target_trail:
                    # Keep riding the run: from the next candle, sell on a fall of target_trail from the high.
                    entry["target_armed"] = True
                else:
                    sell(candle, max(target_price, candle["open"]), "PROFIT_LOCK")
        if entry is not None and not holding_through_crash and exit_signals is not None and exit_signals[index]:
            sell(candle, candle["close"], "EXIT_SIGNAL")
        if (entry is not None and not holding_through_crash and regime_switches and regimes.on_switch == "close"
                and regime_switches[-1]["time"] == candle["start"]):
            sell(candle, candle["close"], "REGIME_SWITCH")
        if entry is not None:
            entry["highest"] = max(entry["highest"], candle["high"])
        if recovered_now:
            sell_the_rebound(candle, episode)

        equity = cash + (units + sum(lot["units"] for lot in flash_lots)) * candle["close"]
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, _fall(equity, peak_equity))
        limit_peak_equity = max(limit_peak_equity, equity)
        record_equity(index, equity)
        max_window_drawdown = max(max_window_drawdown, _fall(equity, window_peak(index)))
        if (not window_limited and not paused and max_drawdown_limit and not holding_through_crash
                and _fall(equity, limit_peak_equity) >= max_drawdown_limit):
            # Pause until the next prime opportunity: the entry signal has to switch off
            # (the trend that failed has ended) and then fire afresh before trading resumes.
            paused, trend_reset = True, False
            drawdown_pauses.append({"paused_at": candle["start"], "resumed_at": None})
            cooldown_ends = index + cooldown_candles if cooldown_candles else None
            if entry is not None:
                sell(candle, candle["close"], "DRAWDOWN_LIMIT")
        if halted_at is None and halt_floor() and equity < halt_floor():
            # Checked at every close, a flash crash's included: a collapse that doesn't recover within its
            # candle stops the model here. (Its intrabar level above is skipped on crash candles, as the
            # other stops are, so a one-off bad print can't stop the model for good.)
            halted_at = candle["start"]
            if entry is not None:
                sell(candle, candle["close"], "LOSS_HALT")
            for crash in {id(lot["episode"]): lot["episode"] for lot in flash_lots}.values():
                sell_the_rebound(candle, crash, "LOSS_HALT")
        if paused and cooldown_ends is not None and index >= cooldown_ends:
            # The cooldown is over: trade again, measuring the limit from the model's value now.
            paused, cooldown_ends = False, None
            drawdown_pauses[-1]["resumed_at"] = candle["start"]
            limit_peak_equity = equity
            window_peaks.clear()
            record_equity(index, equity)
        if paused and not entry_signal:
            trend_reset = True
        if waiting_for_fresh_signal and not entry_signal:
            waiting_for_fresh_signal = False  # the signal has switched off; the next one is fresh

        order_room = max_orders_per_day is None or orders_today < max_orders_per_day
        # With a cooldown, only the cooldown ends a pause; without one, a fresh entry signal does.
        can_enter = ((not paused or (trend_reset and cooldown_candles is None)) and not waiting_for_fresh_signal
                     and halted_at is None)
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
        tax_reserve_series.append(ledger.reserved)

        if progress is not None and (index % report_every == 0 or index == total - 1):
            progress((index + 1) / total, f"{index + 1:,} / {total:,} candles, {len(trades):,} trades")

    last = candles[-1]
    ending_equity = cash + (units + sum(lot["units"] for lot in flash_lots)) * last["close"]
    wins = [trade for trade in trades if trade["net_pnl"] > 0]
    estimated_tax_due = ledger.reserved
    # The strategy's first signal entry; flash-crash fills are opportunistic and reported separately.
    first_entry = next((trade["entry_time"] for trade in trades if trade["entry_reason"] == "ENTRY_SIGNAL"),
                       entry["time"] if entry else None)
    # Buy and hold starts on the first candle the model could have bought on, or at its
    # first buy if that came sooner (an "or" rule can fire before all its parts warm up).
    ready_index = int(entry_ready.argmax()) if entry_ready.any() else None
    starts = [] if ready_index is None else [ready_index]
    if first_entry is not None:
        starts.append(bisect.bisect_left([candle["start"] for candle in candles], first_entry))
    benchmark_index = min(starts, default=0)
    # Where the model stands after the last candle, for the live trader (live_trader.py): its open position, and
    # the stops and profit target that would guard it on the next candle, as the loop above would compute them.
    live_state = {
        "last_candle": last["start"], "entry_signal": bool(entry_signals[-1]), "position": None, "stops": [],
        "profit_target": None, "position_fraction": position_fraction, "paused": paused,
        "halted": halted_at is not None, "orders_today": orders_today, "max_orders_per_day": max_orders_per_day,
    }
    if entry is not None:
        live_state["position"] = {
            "time": entry["time"], "price": entry["price"], "cost": entry["cost"], "units": units,
            "highest": entry["highest"], "reason": entry["reason"], "target_armed": bool(entry.get("target_armed"))}
        live_state["stops"] = [{"price": price, "reason": reason} for price, reason in exit_stops(total)]
        if profit_lock and not entry.get("target_armed"):
            live_state["profit_target"] = {"price": entry["price"] * (1 + profit_lock), "arms_trail": bool(target_trail)}
    return {
        "live_state": live_state,
        "product_id": profile.get("product_id"),
        "compute_device": compute_device,
        "gpu_indicators": list(model.gpu_indicators),
        "requested_start": start_time,
        "waited_for_fresh_signal": wait_for_fresh_signal,
        "warmup_candles": first,
        "first_entry": first_entry,
        "ready_to_trade": candles[ready_index]["start"] if ready_index is not None else None,
        "period_start": candles[0]["start"],
        "period_end": last["start"],
        "candle_count": total,
        "starting_capital": float(starting_capital),
        "ending_equity": ending_equity,
        "total_return": ending_equity / starting_capital - 1,
        "buy_and_hold_start": candles[benchmark_index]["start"],
        "buy_and_hold_return": last["close"] / candles[benchmark_index]["close"] - 1,
        "buy_and_hold_from_test_start": last["close"] / candles[0]["close"] - 1,
        "max_drawdown": max_drawdown,
        "max_window_drawdown": max_window_drawdown,
        "drawdown_window": window_setting,
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "fees_paid": fees_paid,
        "commission_rate": fee_rate,
        "taxes_paid": ledger.paid,
        "taxes_reserved": ledger.reserved,
        "tax_rule": ledger.describe(),
        "tax_payments": tax_payments,
        "estimated_tax_due": estimated_tax_due,
        "screened_signals": screened_signals,
        "flash_crash_buys": [trade for trade in trades if trade["entry_reason"].startswith("FLASH_CRASH_BUY")],
        "flash_crash_events": [
            {key: episode[key] for key in ("start_time", "reference", "low", "minutes")}
            for episode in crash_episodes if episode["end"] is None or episode["end"] >= first],
        "open_flash_lots": len(flash_lots),
        "friction": friction.describe(candles[0]["close"], starting_capital * position_fraction),
        "open_position": entry is not None,
        "drawdown_pauses": drawdown_pauses,
        "halted_at": halted_at,
        "trades": trades,
        "indicators": model.indicator_meta(),
        "rules": model.rule_text(),
        "series": {
            "time": [candle["start"] for candle in candles],
            "close": indicator_series["close"],
            "equity": equity_series,
            "tax_reserve": tax_reserve_series,
            # The active regime on each candle, as an index into result["regimes"]["names"] (profiles with regimes).
            **({"regime": active_regime} if active_regime is not None else {}),
            "indicators": {name: indicator_series[name] for meta in model.indicator_meta()
                           for name in meta["outputs"]},
        },
        "unmodeled_settings": UNMODELED_SETTINGS,
        "regimes": None if regimes is None else {
            "names": regimes.names, "switches": regime_switches,
            "share": {name: active_regime.count(number) / total for number, name in enumerate(regimes.names)},
            "confirm_candles": regimes.confirm, "min_hold_candles": regimes.min_hold, "on_switch": regimes.on_switch,
            "summary": regimes.summary(),
        },
    }

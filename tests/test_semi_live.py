import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

import semi_live
from backtest import run_backtest
from candle_store import CandleStore
from friction import FrictionModel
from tests.test_backtest import _profile

HOUR = 3600
# The session starts 10 minutes into an hour, so the first candle it trades opened before it started.
OPEN_CANDLE = 20_000 * 86_400 + 10 * HOUR  # 10:00 UTC
STARTED = OPEN_CANDLE + 600


def _walk(count, start, first_close=100.0, drift=0.0, seed=7):
    rng = np.random.default_rng(seed)
    closes = first_close * np.exp(np.cumsum(rng.normal(drift, 0.01, count)))
    opens = np.concatenate([[first_close], closes[:-1]])
    return [{"start": start + index * HOUR, "open": float(o), "close": float(c), "high": float(max(o, c) * 1.002),
             "low": float(min(o, c) * 0.998), "volume": 5.0} for index, (o, c) in enumerate(zip(opens, closes))]


def _history(count=600, drift=0.0):
    return _walk(count, OPEN_CANDLE - count * HOUR, drift=drift)


def _hourly(**overrides):
    return _profile(period_granularity="ONE_HOUR", **overrides)


# --- directions ---------------------------------------------------------------------------

def test_directions_split_real_moves_into_five_ranges_with_their_odds():
    targets = semi_live.projection_targets(_history(2000, drift=0.001), HOUR, 24 * HOUR, basis_days=60)

    assert [t["name"] for t in targets] == [name for _, name in semi_live.DIRECTIONS]
    moves = [t["move"] for t in targets]
    assert moves == sorted(moves, reverse=True)
    assert targets[2]["move"] > 0  # a rising market projects a rising middle
    assert all(t["change"] == pytest.approx(math.exp(t["move"]) - 1) for t in targets)
    assert "real 1d moves" in targets[0]["method"]
    odds = [t["probability"] for t in targets]
    assert sum(odds) == pytest.approx(1.0)
    assert odds[2] > odds[0] and odds[2] > odds[4]  # the middle range holds the most of history


def test_the_odds_follow_the_live_price():
    odds = semi_live.DirectionOdds(_history(3000), HOUR, 24 * HOUR, basis_days=120)
    start = odds.at(0, 0.0)
    assert start == pytest.approx([t["probability"] for t in odds.targets])

    rising, falling = odds.at(12 * HOUR, 0.03), odds.at(12 * HOUR, -0.03)

    assert sum(rising) == pytest.approx(1.0) and sum(falling) == pytest.approx(1.0)
    assert rising[0] + rising[1] > start[0] + start[1] and falling[3] + falling[4] > start[3] + start[4]
    assert rising[0] + rising[1] > falling[0] + falling[1]
    done = odds.at(24 * HOUR, odds.targets[1]["move"])  # at the end the move decides it
    assert max(done) == done[1]


def test_a_session_shorter_than_a_candle_scales_the_moves_down():
    daily = [{**candle, "start": index * 86_400} for index, candle in enumerate(_history(2000))]
    whole_day = semi_live.projection_targets(daily, 86_400, 86_400)
    one_hour = semi_live.projection_targets(daily, 86_400, HOUR)

    assert one_hour[0]["move"] == pytest.approx(whole_day[0]["move"] * math.sqrt(1 / 24))


def test_too_little_history_falls_back_to_a_normal_distribution():
    odds = semi_live.DirectionOdds(_history(20), HOUR, 24 * HOUR)
    targets = odds.targets

    assert "normal distribution" in targets[0]["method"]
    assert targets[0]["move"] > targets[2]["move"] > targets[4]["move"]
    assert sum(t["probability"] for t in targets) == pytest.approx(1.0)
    assert targets[2]["probability"] > targets[0]["probability"]
    rising = odds.at(12 * HOUR, 0.05)
    assert rising[0] > targets[0]["probability"] and sum(rising) == pytest.approx(1.0)


def test_the_closest_direction_is_the_one_the_prices_follow():
    targets = semi_live.projection_targets(_history(2000), HOUR, 24 * HOUR)
    period = 24 * HOUR
    rally = [(STARTED + step * 600, 100 * (1 + semi_live.projected_change(targets[1], step * 600 / period)))
             for step in range(1, 40)]

    closest, distances = semi_live.closest_direction(targets, STARTED, 100.0, period, rally)

    assert closest == 1 and distances[1] == pytest.approx(0.0, abs=1e-12)
    assert semi_live.closest_direction(targets, STARTED, 100.0, period, []) == (None, [])


def test_lines_start_at_the_price_and_end_on_each_direction():
    targets = semi_live.projection_targets(_history(2000), HOUR, 24 * HOUR)

    lines = semi_live.projection_lines(targets, STARTED, 50.0, 24 * HOUR, points=5)

    assert lines["time"][0] == STARTED and lines["time"][-1] == STARTED + 24 * HOUR
    for target, line in zip(targets, lines["lines"]):
        assert line[0] == pytest.approx(50.0) and line[-1] == pytest.approx(50.0 * (1 + target["change"]))


# --- projections --------------------------------------------------------------------------

def test_session_candles_are_those_closing_during_the_session():
    assert semi_live.session_candles(HOUR, STARTED, 4 * HOUR) == (OPEN_CANDLE, 4)
    just_after_midnight = 20_000 * 86_400 + 600
    assert semi_live.session_candles(86_400, just_after_midnight, HOUR) == (20_000 * 86_400, 0)
    assert semi_live.session_candles(86_400, just_after_midnight, 86_400 - 601)[1] == 0
    assert semi_live.session_candles(86_400, just_after_midnight, 86_400 - 600)[1] == 1


def test_a_projected_path_continues_history_and_ends_on_its_direction():
    history = _history()
    target = {"move": math.log(1.2)}

    candles, start = semi_live.build_projection(history, HOUR, target, 24, direction=0, path=0)

    made = candles[len(history):]
    assert start == OPEN_CANDLE and len(made) == 24 and candles[:len(history)] == history
    assert made[-1]["close"] / history[-1]["close"] == pytest.approx(1.2)
    assert all(b["start"] - a["start"] == HOUR for a, b in zip(made, made[1:]))
    assert getattr(candles, "synthetic", False)
    again, _ = semi_live.build_projection(history, HOUR, target, 24, direction=0, path=0)
    other, _ = semi_live.build_projection(history, HOUR, target, 24, direction=0, path=1)
    assert again[-5:] == candles[-5:] and other[-5:] != candles[-5:]


def test_projections_run_every_model_on_every_path_of_every_direction():
    history = _history()
    hourly, daily = _hourly(), _profile(period_granularity="ONE_DAY")
    models = [("hourly", hourly), ("daily", daily)]
    frictions = {name: FrictionModel(profile) for name, profile in models}
    markets = {("BTC-USD", "ONE_HOUR"): history, ("BTC-USD", "ONE_DAY"): history}
    targets = semi_live.projection_targets(history, HOUR, 6 * HOUR)
    progress = []

    out = semi_live.run_projections(models, frictions, markets, targets, STARTED, 6 * HOUR, paths=3,
                                    progress=lambda fraction, message: progress.append(fraction))

    assert out["daily"] is None  # no daily candle closes in six hours
    assert len(out["hourly"]) == 5 and all(direction["paths"] == 3 for direction in out["hourly"])
    assert all(d["worst"] <= d["median"] <= d["best"] for d in out["hourly"])
    assert progress[-1] == 1.0 and len(progress) == 15
    curve = out["hourly"][0]["curve"]
    assert curve["time"][0] == OPEN_CANDLE + HOUR and len(curve["time"]) == 6


def test_expected_by_reads_the_curve_at_a_moment():
    curve = {"time": [10, 20, 30], "return": [0.01, 0.02, 0.03]}

    assert semi_live.expected_by(curve, 5) == 0.0
    assert semi_live.expected_by(curve, 25) == 0.02
    assert semi_live.expected_by(curve, 99) == 0.03


# --- watching the market --------------------------------------------------------------------

class FakeMarket:
    """A Coinbase client whose candles exist only once they have closed on a fake clock."""

    def __init__(self, candles, clock, price_fails=False):
        self.candles, self.clock, self.price_fails = candles, clock, price_fails

    def get_product(self, product_id):
        if self.price_fails:
            raise RuntimeError("down")
        closed = [c for c in self.candles if c["start"] + HOUR <= self.clock[0]]
        return SimpleNamespace(price=str(closed[-1]["close"] * 1.001))

    def get_candles(self, product_id, start, end, granularity, limit):
        out = [SimpleNamespace(**{k: str(v) for k, v in c.items()}) for c in self.candles
               if int(start) < c["start"] <= int(end) and c["start"] + HOUR <= self.clock[0]]
        return SimpleNamespace(candles=list(reversed(out)))


class FakeStop:
    """Advances the fake clock instead of waiting."""

    def __init__(self, clock, stop_at=None):
        self.clock, self.stop_at = clock, stop_at

    def wait(self, seconds):
        self.clock[0] += seconds
        return self.stop_at is not None and self.clock[0] >= self.stop_at


def _session(tmp_path, history, horizon=4 * HOUR, models=None, product="BTC-USD"):
    models = models or [("ema", _hourly(product_id=product))]
    store = CandleStore(tmp_path)
    store.save(product, "ONE_HOUR", history, 0, OPEN_CANDLE)
    from backtest import load_cached_candles
    candles = load_cached_candles(tmp_path, product, "ONE_HOUR")[0]
    markets = {(product, "ONE_HOUR"): candles}
    frictions = {name: FrictionModel(profile) for name, profile in models}
    targets = semi_live.projection_targets(candles, HOUR, horizon)
    projections = semi_live.run_projections(models, frictions, markets, targets, STARTED, horizon, paths=2)
    return semi_live.LiveSession(product, models, frictions, markets, targets, projections, STARTED, horizon,
                                 candles[-1]["close"]), models


def test_monitoring_paper_trades_each_closed_candle_as_a_backtest_would(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.time.sleep", lambda _seconds: None)
    world = _walk(610, OPEN_CANDLE - 600 * HOUR)
    history, future = world[:600], world[600:]
    session, models = _session(tmp_path, history)
    clock = [STARTED]
    updates = []

    complete = semi_live.monitor(FakeMarket(world, clock), session, FakeStop(clock), 300, updates.append,
                                 now=lambda: clock[0], cache_dir=tmp_path)

    assert complete
    final = updates[-1]
    assert final["finished"] and final["now"] == pytest.approx(session.ends)
    live = final["rows"][0]["live"]
    expected = run_backtest(history + future[:4], models[0][1], friction=FrictionModel(models[0][1]),
                            start_time=OPEN_CANDLE)
    assert live["candles"] == 4
    assert live["total_return"] == pytest.approx(expected["total_return"])
    assert live["trade_count"] == expected["trade_count"]
    # Candles were fetched only after they closed and settled, each once, and never one closing after the end.
    stored = CandleStore(tmp_path).load("BTC-USD", "ONE_HOUR")[0]
    assert [c["start"] for c in stored[-4:]] == [c["start"] for c in future[:4]]
    assert final["closest"] is not None and final["rows"][0]["expected"] is not None
    assert len(final["ticks"]) > 40  # a price every 5 minutes for 4 hours, and each candle's close


def test_stopping_ends_the_session_early_and_a_failing_price_falls_back_to_candles(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.time.sleep", lambda _seconds: None)
    world = _walk(610, OPEN_CANDLE - 600 * HOUR)
    session, _ = _session(tmp_path, world[:600])
    clock = [STARTED]

    complete = semi_live.monitor(FakeMarket(world, clock, price_fails=True), session,
                                 FakeStop(clock, stop_at=STARTED + 2 * HOUR), 600, lambda _snapshot: None,
                                 now=lambda: clock[0], cache_dir=tmp_path)

    assert not complete
    snapshot = session.snapshot(clock[0], stopped=True)
    assert snapshot["stopped"] and not snapshot["finished"]
    assert snapshot["rows"][0]["live"]["candles"] == 1  # the second candle closes at 2h but hasn't settled yet
    assert [price for _, price in session.ticks[1:]] == [world[600]["close"]]
    assert "stopped" in semi_live.describe(snapshot)


def test_the_session_record_has_every_price_and_each_direction(tmp_path):
    session, _ = _session(tmp_path, _history())
    session.add_tick(STARTED + 60, 101.0)
    snapshot = session.snapshot(STARTED + 60)

    folder = semi_live.write_session(snapshot, tmp_path / "record")

    rows = (folder / "prices.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 + len(snapshot["ticks"]) and "Strong rally" in rows[0]
    assert (folder / "session.json").exists()
    assert snapshot["product_id"] in semi_live.describe(snapshot)


# --- settings ----------------------------------------------------------------------------------

def test_settings_are_checked():
    assert semi_live.settings_of({})[1:] == (86_400, 30.0, 5, 365.0)
    assert semi_live.parse_duration("90m") == 5400 and semi_live.parse_duration("2w") == 14 * 86_400
    for bad in ({"semi_live_period": "soon"}, {"semi_live_poll_seconds": 1}, {"semi_live_paths": 0},
                {"semi_live_basis_days": "x"}):
        with pytest.raises(semi_live.SemiLiveError):
            semi_live.settings_of(bad)


def test_every_selected_currency_is_followed_unless_one_is_set():
    btc, eth = ("b", _profile()), ("e", _profile(product_id="ETH-USD"))

    assert semi_live.pick_models([btc, eth], "") == ([btc, eth], ["BTC-USD", "ETH-USD"])
    assert semi_live.pick_models([btc, eth], "ETH-USD") == ([eth], ["ETH-USD"])
    with pytest.raises(semi_live.SemiLiveError, match="SOL-USD"):
        semi_live.pick_models([btc], "SOL-USD")


def test_sessions_for_several_currencies_run_together(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.time.sleep", lambda _seconds: None)
    world = _walk(610, OPEN_CANDLE - 600 * HOUR)
    btc, _ = _session(tmp_path, world[:600])
    eth, _ = _session(tmp_path, world[:600], models=[("eth_ema", _hourly(product_id="ETH-USD"))], product="ETH-USD")
    clock = [STARTED]
    updates = []

    complete = semi_live.monitor(FakeMarket(world, clock), [btc, eth], FakeStop(clock), 300, updates.append,
                                 now=lambda: clock[0], cache_dir=tmp_path)

    assert complete
    finals = {u["product_id"]: u for u in updates if u["finished"]}
    assert set(finals) == {"BTC-USD", "ETH-USD"}
    assert finals["ETH-USD"]["rows"][0]["model"] == "eth_ema" and finals["ETH-USD"]["rows"][0]["live"]["candles"] == 4
    line = semi_live.describe_all(finals.values())
    assert "BTC-USD" in line and "ETH-USD" in line and "finished" in line
    assert semi_live.describe_all([finals["BTC-USD"]]) == semi_live.describe(finals["BTC-USD"])


def test_the_test_options_describe_the_mode():
    from scenario_editor import DEFAULT_OPTIONS, MODES, describe_mode

    assert ("semi-live", "Semi-live monitoring") in MODES
    text = describe_mode({**DEFAULT_OPTIONS, "mode": "semi-live", "semi_live_currency": "BTC-USD"})
    assert "BTC-USD" in text and "1d" in text


def test_projections_on_worker_processes_match_one_after_another(tmp_path):
    history = _history()
    CandleStore(tmp_path).save("BTC-USD", "ONE_HOUR", history, 0, OPEN_CANDLE)
    models = [("a", _hourly()), ("b", _hourly(indicators={"fast_ema": {"type": "EMA", "period": 3},
                                                           "slow_ema": {"type": "EMA", "period": 8},
                                                           "emergency_ema": {"type": "EMA", "period": 1}}))]
    frictions = {name: FrictionModel(profile) for name, profile in models}
    markets = {("BTC-USD", "ONE_HOUR"): history}
    targets = semi_live.projection_targets(history, HOUR, 12 * HOUR)

    serial = semi_live.run_projections(models, frictions, markets, targets, STARTED, 12 * HOUR, paths=2)
    parallel = semi_live.run_projections(models, frictions, markets, targets, STARTED, 12 * HOUR, paths=2,
                                         workers=3, cache_dir=tmp_path)

    for name in ("a", "b"):
        assert [d["median"] for d in parallel[name]] == pytest.approx([d["median"] for d in serial[name]])
        assert [d["curve"] for d in parallel[name]] == [d["curve"] for d in serial[name]]


def test_the_session_record_keeps_the_odds(tmp_path):
    session, _ = _session(tmp_path, _history(), horizon=6 * HOUR)
    snapshot = session.snapshot(STARTED + 60)

    assert snapshot["odds"] == [t["probability"] for t in snapshot["targets"]]
    assert snapshot["likely"] == int(np.argmax(snapshot["odds"]))
    record = json.loads((semi_live.write_session(snapshot, tmp_path / "r") / "session.json").read_text())
    assert record["odds"] == pytest.approx(snapshot["odds"])


def test_the_session_carries_on_the_backtest_chart(tmp_path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import date2num
    from matplotlib.figure import Figure
    from chart import BacktestChart
    history = _history()
    session, models = _session(tmp_path, history, horizon=6 * HOUR)
    session.odds = semi_live.DirectionOdds(history, HOUR, 6 * HOUR)
    session.add_tick(STARTED + 600, history[-1]["close"] * 1.01)
    snapshot = session.snapshot(STARTED + 600)
    result = run_backtest(history, models[0][1], friction=FrictionModel(models[0][1]))
    figure = Figure(figsize=(10, 6))
    chart = BacktestChart(figure, FigureCanvasAgg(figure))
    chart.show(result)
    before = chart.view_range()

    chart.show_live(snapshot)

    first, last = chart.view_range()
    assert first == before[0] and last == pytest.approx(date2num(np.datetime64(session.ends, "s")))
    labels = [text.get_text() for text in chart.ax.texts]
    for target, odds in zip(snapshot["targets"], snapshot["odds"]):
        assert any(label.startswith(f"{odds:.0%} {target['name']}") for label in labels)
    lo, hi = chart.session_window()
    assert hi == pytest.approx(last) and hi - lo == pytest.approx(18 / 24)  # the session and twice it before
    chart.show_window(lo, hi)
    low, high = chart.ax.get_ylim()
    assert low < min(min(line) for line in snapshot["lines"]["lines"])
    assert high > max(max(line) for line in snapshot["lines"]["lines"])

    chart.show(result)  # another redraw keeps the session on
    assert len(chart.ax.texts) >= 5
    chart.show_live(None)
    assert chart.view_range() == before and chart.session_window() is None


def test_the_direction_labels_hide_when_the_session_is_a_sliver_of_the_chart(tmp_path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.text import Annotation
    from chart import BacktestChart
    history = _history()
    session, models = _session(tmp_path, history, horizon=6 * HOUR)
    figure = Figure(figsize=(10, 6))
    chart = BacktestChart(figure, FigureCanvasAgg(figure))
    chart.show(run_backtest(history, models[0][1], friction=FrictionModel(models[0][1])))
    chart.show_live(session.snapshot(STARTED + 60))
    labels = [artist for artist in chart._live_artists if isinstance(artist, Annotation)]

    chart.show_window(*chart.session_window())
    assert labels and all(label.get_visible() for label in labels)
    first, last = chart.view_range()
    chart.show_window(first, last)  # 25 days with a 6-hour session
    assert not any(label.get_visible() for label in labels)


def test_the_chart_shows_the_session_of_the_shown_models_currency(tmp_path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from chart import BacktestChart
    history = _history()
    btc, models = _session(tmp_path, history, horizon=6 * HOUR)
    eth, _ = _session(tmp_path, history, horizon=6 * HOUR, product="ETH-USD")
    figure = Figure(figsize=(10, 6))
    chart = BacktestChart(figure, FigureCanvasAgg(figure))
    chart.show(run_backtest(history, models[0][1], friction=FrictionModel(models[0][1])))

    chart.show_live(eth.snapshot(STARTED + 60))
    assert chart.live is None and not chart._live_artists  # an ETH session isn't drawn on a BTC model
    chart.show_live(btc.snapshot(STARTED + 60))
    assert chart.live["product_id"] == "BTC-USD" and chart._live_artists
    chart.clear_live()
    chart.show(chart.result)
    assert chart.live is None and not chart._live_artists


def test_the_history_runs_into_the_live_price_through_the_lead_in():
    day = 86_400
    snapshot = {"started": 10 * day + 5 * HOUR, "ticks": [(10 * day + 5 * HOUR, 105.0), (10 * day + 6 * HOUR, 106.0)],
                "lead_in": [(8 * day + h * HOUR, 90.0 + h) for h in range(1, 54)]}

    times, prices = semi_live.bridge(snapshot, 9 * day, 100.0, candle_seconds=day)

    # from the daily chart's last point, through each hourly close after that candle closed, to the first tick
    assert times[0] == 9 * day and prices[0] == 100.0
    assert times[1] == 10 * day + HOUR and times[-2] == 10 * day + 4 * HOUR
    assert (times[-1], prices[-1]) == snapshot["ticks"][0]
    assert semi_live.bridge({**snapshot, "lead_in": []}, 9 * day, 100.0, day) == ([9 * day, snapshot["started"]],
                                                                                  [100.0, 105.0])


def test_a_session_keeps_the_prices_leading_into_it(tmp_path):
    history = _history()
    session, models = _session(tmp_path, history)
    lead = semi_live.LiveSession("BTC-USD", models, {}, {("BTC-USD", "ONE_HOUR"): history}, session.targets, {},
                                 STARTED, 4 * HOUR, 100.0, lead_in=(history, HOUR))

    times = [t for t, _ in lead.snapshot(STARTED)["lead_in"]]
    assert times[-1] == OPEN_CANDLE and times[0] >= STARTED - semi_live.LEAD_IN_DAYS * 86_400
    assert len(times) == semi_live.LEAD_IN_DAYS * 24


def test_the_indicators_carry_on_through_the_session_with_the_open_candle(tmp_path):
    from model_config import CompiledModel
    history = _history()
    session, models = _session(tmp_path, history)
    session.add_tick(STARTED + 600, history[-1]["close"] * 1.02)
    session.add_tick(STARTED + 900, history[-1]["close"] * 1.01)

    live = session.snapshot(STARTED + 900)["rows"][0]["indicators"]

    # from the backtest's last candle to the open one as it stands now
    assert live["time"] == [history[-1]["start"], STARTED + 900]
    forming = {"start": OPEN_CANDLE, "open": history[-1]["close"], "high": history[-1]["close"] * 1.02,
               "low": history[-1]["close"], "close": history[-1]["close"] * 1.01,
               "volume": 5.0 * (STARTED + 900 - OPEN_CANDLE) / HOUR}  # the average volume, for the time gone
    expected = CompiledModel(models[0][1], HOUR).evaluate(history + [forming])[0]
    assert live["close"][-1] == pytest.approx(history[-1]["close"] * 1.01)
    for output, values in live["values"].items():
        assert values == pytest.approx([float(v) for v in expected[output][-2:]], nan_ok=True)


def test_closed_candles_join_the_live_indicators(tmp_path, monkeypatch):
    monkeypatch.setattr("backtest.time.sleep", lambda _seconds: None)
    world = _walk(610, OPEN_CANDLE - 600 * HOUR)
    session, _ = _session(tmp_path, world[:600])
    clock = [STARTED]
    updates = []

    semi_live.monitor(FakeMarket(world, clock), session, FakeStop(clock, stop_at=STARTED + 2 * HOUR + 600), 300,
                      updates.append, now=lambda: clock[0], cache_dir=tmp_path)

    live = updates[-1]["rows"][0]["indicators"]
    closed = [world[599]["start"], OPEN_CANDLE, OPEN_CANDLE + HOUR]
    assert live["time"][:3] == closed and live["time"][-1] == updates[-1]["now"]

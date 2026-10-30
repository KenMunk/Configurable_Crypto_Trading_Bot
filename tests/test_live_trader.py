"""The live trader (live_trader.py): many models on many currencies, following the backtest's decisions."""
import json
import threading
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import live_trader
from backtest import run_backtest
from live_trader import (CoinbaseBroker, LiveConfigError, LiveTrader, OrderUnknown, PaperBroker, TradingLedger,
                         floor_to, load_live_config)
from tests.test_backtest import _profile

HOUR = 3600
T0 = 20_000 * 86_400  # a midnight, UTC
ENV = {"coinbase": {}, "live_execution_active": False}


def _series(closes, start=T0, lows=None):
    candles, previous = [], closes[0]
    for index, close in enumerate(closes):
        low = min(previous, close) if lows is None or index not in lows else lows[index]
        candles.append({"start": start + index * HOUR, "open": previous, "high": max(previous, close), "low": low,
                        "close": close, "volume": 1.0})
        previous = close
    return candles


# BTC: flat, a rise from candle 60 (the EMA cross enters there), then a fall (the cross exits).
RISE = [100.0] * 60 + [100.0 + step for step in range(1, 11)] + [110.0 - step for step in range(1, 11)] + [100.0] * 20
FLAT = [50.0] * 110


def _hourly(fraction=1.0, **overrides):
    profile = _profile(period_granularity="ONE_HOUR", capital_allocation={"target_position_fraction": fraction},
                       **overrides)
    profile["friction_model"]["tax_model"] = "flat"
    return profile


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeMarket:
    """Prices set by the test; every product trades in steps of a hundred-millionth."""

    def __init__(self):
        self.prices = {}
        self.info = {"base_increment": "0.00000001", "quote_increment": "0.01", "base_min_size": 0.0,
                     "quote_min_size": 1.0, "status": "online"}

    def quotes(self, products, now):
        return {p: {"bid": price, "ask": price, "mid": price, "time": now}
                for p, price in self.prices.items() if p in products}

    def product(self, product_id, now, refresh=False):
        return dict(self.info)


class RecordingBroker(PaperBroker):
    """A live broker that fills like paper, records each order and reports balances."""

    mode = "live"

    def __init__(self, balances=None):
        self.orders, self.fills = [], {}
        self.balance = balances or {"USD": (1e9, 1e9), "BTC": (1e9, 1e9), "ETH": (1e9, 1e9)}

    def balances(self):
        return self.balance

    def buy(self, product_id, *args):
        self.orders.append(("BUY", product_id, args[0]))
        return super().buy(product_id, *args)

    def sell(self, product_id, *args):
        self.orders.append(("SELL", product_id, args[0]))
        return super().sell(product_id, *args)

    def await_order(self, order_id):
        return self.fills[order_id]


class Harness:
    """A trader on fake candles, prices and clock. `candles` maps each product to its full history; the trader
    sees each candle once it has closed."""

    def __init__(self, tmp_path, monkeypatch, deployments, profiles, candles, env=ENV, safeguards=None,
                 start=T0 + 55 * HOUR + 600, live_broker=None, mode="paper"):
        self.tmp_path, self.candles = tmp_path, candles
        for name, profile in profiles.items():
            (tmp_path / f"{name}.json").write_text(json.dumps(profile), encoding="utf-8")
        for item in deployments:
            item.setdefault("profile", str(tmp_path / f"{item['id']}.json"))
        self.config_path = tmp_path / "live_config" / "live_trading.json"
        self.config_path.parent.mkdir(exist_ok=True)
        self.config_path.write_text(json.dumps({"deployments": deployments, "global_safeguards": safeguards or {}}),
                                    encoding="utf-8")
        monkeypatch.setattr(live_trader, "load_candles", self._load)
        monkeypatch.setattr(live_trader, "fetch_friction_inputs", lambda client, profiles: {})
        self.clock, self.market = Clock(start), FakeMarket()
        self.mode = mode  # chosen by the API key in real use (live_trader.check_key)
        self.ledger = TradingLedger(tmp_path / mode / "ledger" / "trading_ledger.db")
        self.env, self.live_broker = env, live_broker
        self.trader = self.make()

    def make(self):
        return LiveTrader(load_live_config(self.config_path), client=None, ledger=self.ledger, env=self.env,
                          base_dir=self.tmp_path, now=self.clock, market_data=self.market,
                          live_broker=self.live_broker or RecordingBroker(), mode=self.mode)

    def _load(self, client, product_id, granularity, start, end, cache_dir=None):
        return [c for c in self.candles[product_id] if start <= c["start"] < end]

    def price_at(self, hour):
        """Set every product's price to the close of the candle that just closed before `hour`."""
        for product, candles in self.candles.items():
            self.market.prices[product] = candles[hour - 1]["close"]

    def run_to(self, hour, offset=31):
        """Step the trader at each candle close up to `hour`, with the prices at each."""
        first = int((self.clock.t - T0) // HOUR) + 1
        for h in range(first, hour + 1):
            self.clock.t = T0 + h * HOUR + offset
            self.price_at(h)
            self.trader.step()

    def row(self, deployment_id):
        return self.ledger.deployment(deployment_id)


def _closed(row):
    """A position sold: no entry, and at most dust below the size step left over."""
    return row["entry_time"] is None and row["units"] < 1e-7


def _harness(tmp_path, monkeypatch, **kwargs):
    deployments = kwargs.pop("deployments", [
        {"id": "btc_all_in", "capital": 1000}, {"id": "btc_half", "capital": 500}, {"id": "eth_flat", "capital": 800}])
    profiles = kwargs.pop("profiles", {
        "btc_all_in": _hourly(1.0), "btc_half": _hourly(0.5), "eth_flat": _hourly(1.0, product_id="ETH-USD")})
    candles = kwargs.pop("candles", {"BTC-USD": _series(RISE), "ETH-USD": _series(FLAT)})
    harness = Harness(tmp_path, monkeypatch, deployments, profiles, candles, **kwargs)
    harness.price_at(55)
    harness.trader.start()
    return harness


# --- several models on several currencies -----------------------------------------------------------

def test_models_on_several_currencies_trade_side_by_side_with_their_own_money(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    assert {row["id"] for row in h.ledger.deployments()} == {"btc_all_in", "btc_half", "eth_flat"}
    assert all(row["mode"] == "paper" for row in h.ledger.deployments())

    h.run_to(61)  # candle 60 closed: the rise crossed the EMAs

    all_in, half, eth = h.row("btc_all_in"), h.row("btc_half"), h.row("eth_flat")
    assert all_in["units"] == pytest.approx(1000 / 101) and all_in["cash"] == pytest.approx(0.0, abs=1e-9)
    assert half["units"] == pytest.approx(250 / 101) and half["cash"] == pytest.approx(250.0)
    assert eth["units"] == 0 and eth["cash"] == 800  # its market never signalled

    h.run_to(90)  # through the fall: the cross exits both BTC models

    trades = h.ledger.trades()
    assert {t["deployment_id"] for t in trades} == {"btc_all_in", "btc_half"}
    assert all(t["exit_reason"] == "EXIT_SIGNAL" and t["pnl"] > 0 for t in trades)
    assert _closed(h.row("btc_all_in")) and h.row("btc_all_in")["cash"] > 1000


def test_the_real_account_matches_a_backtest_started_at_deployment(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(90)
    started = h.row("btc_all_in")["started_at"]
    expected = run_backtest(_series(RISE)[:90], _hourly(1.0), starting_capital=1000, start_time=started,
                            wait_for_fresh_signal=True)

    trades = h.ledger.trades("btc_all_in")
    assert len(trades) == expected["trade_count"] == 1
    assert trades[0]["exit_price"] == pytest.approx(expected["trades"][0]["exit_price"])
    assert h.row("btc_all_in")["cash"] == pytest.approx(expected["ending_equity"])


def test_the_dashboard_snapshot_lists_every_deployment(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(62)
    snapshot = h.trader.snapshot()

    assert snapshot["currencies"] == ["BTC-USD", "ETH-USD"]
    rows = {row["id"]: row for row in snapshot["deployments"]}
    assert rows["btc_all_in"]["position"] is not None and rows["btc_all_in"]["in_sync"]
    assert rows["eth_flat"]["position"] is None and rows["eth_flat"]["replay"]["in_position"] is False
    assert "btc_half" in live_trader.describe_snapshot(snapshot)


def test_a_restart_reaches_the_same_decisions(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(63)
    before = h.row("btc_all_in")

    h.trader = h.make()  # a fresh process on the same ledger
    h.trader.start()
    h.run_to(66)

    assert h.row("btc_all_in")["units"] == pytest.approx(before["units"])
    assert len([o for o in h.ledger.recent_orders() if o["deployment_id"] == "btc_all_in"]) == 1


# --- exits between candle closes ----------------------------------------------------------------------

def test_a_stop_sells_on_the_live_price_and_the_account_follows_the_backtest_after(tmp_path, monkeypatch):
    stopped = _hourly(1.0, safeguards={"use_hard_stop": True, "hard_stop_percentage": 0.05})
    candles = {"BTC-USD": _series(RISE, lows={61: 95.0})}  # candle 61 dips through the stop
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc_stop", "capital": 1000}],
                 profiles={"btc_stop": stopped}, candles=candles)
    h.run_to(61)
    assert h.row("btc_stop")["units"] > 0

    h.clock.t = T0 + 61 * HOUR + 1200  # 20 minutes into candle 61
    h.market.prices["BTC-USD"] = 95.0
    h.trader.step()

    trade = h.ledger.trades("btc_stop")[0]
    assert trade["exit_reason"] == "HARD_STOP" and _closed(h.row("btc_stop"))

    h.run_to(62)  # the backtest stopped out too, then re-entered at candle 61's close: so does the account
    assert h.row("btc_stop")["units"] == pytest.approx(1000 / 101 * 95 / 102, rel=1e-6)
    assert len(h.ledger.trades("btc_stop")) == 1


def test_live_state_reports_the_stops_the_backtest_uses():
    profile = _hourly(1.0, safeguards={"use_hard_stop": True, "hard_stop_percentage": 0.05})
    profile["strategy"]["exit_signal"].update(trailing_stop=0.1, profit_lock_target=0.2)
    result = run_backtest(_series(RISE[:65]), profile, starting_capital=1000)

    state = result["live_state"]
    assert state["position"]["price"] == 101.0
    stops = {stop["reason"]: stop["price"] for stop in state["stops"]}
    assert stops["HARD_STOP"] == pytest.approx(101 * 0.95)
    assert stops["TRAILING_STOP"] == pytest.approx(105 * 0.9)  # the highest high so far
    assert state["profit_target"] == {"price": pytest.approx(101 * 1.2), "arms_trail": False}


# --- skipping entries ------------------------------------------------------------------------------

def test_an_entry_too_old_to_follow_is_skipped(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.clock.t = T0 + 64 * HOUR + 31  # the trader was down while the model entered at candle 60
    h.price_at(64)
    h.trader.step()

    assert h.row("btc_all_in")["units"] == 0
    assert h.row("btc_all_in")["skipped_entry"] == T0 + 60 * HOUR
    assert any("too long ago" in e["message"] for e in h.ledger.events())


def test_an_entry_the_price_ran_away_from_is_skipped(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, safeguards={"max_entry_price_deviation": 0.01})
    h.run_to(60)
    h.clock.t = T0 + 61 * HOUR + 31
    h.market.prices["BTC-USD"] = 105.0  # 4% above the model's entry at 101
    h.trader.step()

    assert h.row("btc_all_in")["units"] == 0
    assert any("from the model's entry" in e["message"] for e in h.ledger.events())


def test_the_halt_file_stops_entries_but_not_exits(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)
    (h.config_path.parent / "HALT").write_text("", encoding="utf-8")
    h.run_to(90)

    assert _closed(h.row("btc_all_in")) and len(h.ledger.trades("btc_all_in")) == 1

    (tmp_path / "second").mkdir()
    h2 = _harness(tmp_path / "second", monkeypatch)
    (h2.config_path.parent / "HALT").write_text("", encoding="utf-8")
    h2.run_to(61)
    assert h2.row("btc_all_in")["units"] == 0
    assert any("HALT file" in e["message"] for e in h2.ledger.events())


def test_tax_set_aside_on_a_gain_is_not_spent_on_the_next_entry(tmp_path, monkeypatch):
    taxed = _hourly(1.0)
    taxed["friction_model"]["tax_buffer_rate"] = 0.5
    closes = RISE + [100.0 + step for step in range(1, 11)]  # a second rise after the first round trip
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc_taxed", "capital": 1000}],
                 profiles={"btc_taxed": taxed}, candles={"BTC-USD": _series(closes)})
    h.run_to(105)

    first, = h.ledger.trades("btc_taxed")
    row = h.row("btc_taxed")
    reserved = first["pnl"] * 0.5
    cash_after_first = 1000 + first["pnl"]
    assert row["cash"] == pytest.approx(reserved)  # everything but the reserve went into the second position
    assert row["units"] == pytest.approx((cash_after_first - reserved) / 101)


def test_the_order_value_cap_limits_each_buy(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, safeguards={"max_order_value": 300})
    h.run_to(61)
    assert h.row("btc_all_in")["units"] == pytest.approx(300 / 101)
    assert h.row("btc_half")["units"] == pytest.approx(250 / 101)


# --- the combined drawdown limit -------------------------------------------------------------------

def test_a_combined_fall_halts_entries_but_adding_a_deployment_does_not(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, safeguards={"max_global_drawdown": 0.1})
    h.run_to(61)
    h.ledger.set_meta("global_index_paper", {**h.ledger.get_meta("global_index_paper"), "members": ["other"]})
    h.trader.check_global_drawdown(h.clock.t)  # a change of members starts a new baseline, not a loss
    assert not h.ledger.get_meta("global_halt_paper")

    h.market.prices["BTC-USD"] = 60.0  # the BTC models lose 40%, the whole book over a fifth
    h.trader.poll(h.clock.t)
    h.trader.check_global_drawdown(h.clock.t)
    assert "past the 10% limit" in h.ledger.get_meta("global_halt_paper")["reason"]
    assert "halted" in h.trader.entries_blocked(h.trader.deployments["eth_flat"])

    live_trader.main(["--data-dir", str(tmp_path), "--clear-halt", "paper"])
    assert h.trader.entries_blocked(h.trader.deployments["eth_flat"]) is None
    assert h.ledger.get_meta("global_index_paper") is None


# --- live or paper: the API key decides ---------------------------------------------------------------

def _live_profile(**overrides):
    return _hourly(1.0, **overrides)


class KeyClient:
    """Answers the key-permissions request as Coinbase would, or fails as the network or Coinbase can."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), 0

    def get_api_key_permissions(self):
        self.asked += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(**answer)


TRADE_KEY = {"can_view": True, "can_trade": True, "can_transfer": False, "portfolio_type": "DEFAULT"}
VIEW_KEY = {"can_view": True, "can_trade": False, "can_transfer": False, "portfolio_type": "DEFAULT"}


def test_a_key_that_can_trade_trades_live_and_a_view_only_key_paper_trades():
    assert live_trader.check_key(KeyClient(TRADE_KEY)) == ("live", {**TRADE_KEY, "forced_paper": False,
                                                                     "reason": "the API key can trade"})
    mode, key = live_trader.check_key(KeyClient(VIEW_KEY))
    assert mode == "paper" and key["reason"] == "the API key is view-only"
    mode, key = live_trader.check_key(KeyClient(TRADE_KEY), force_paper=True)
    assert mode == "paper" and key["forced_paper"] and "asked for" in key["reason"]


def test_a_refused_key_stops_and_a_missing_network_is_waited_out():
    from requests import Response
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import HTTPError

    refused = Response()
    refused.status_code = 401
    with pytest.raises(LiveConfigError, match="refused the API key"):
        live_trader.check_key(KeyClient(HTTPError(response=refused)))
    with pytest.raises(live_trader.NoNetwork):
        live_trader.check_key(KeyClient(RequestsConnectionError("no route to host")))

    stop, waits, log = threading.Event(), [], []
    stop.wait = lambda seconds: waits.append(seconds) or False
    client = KeyClient(RequestsConnectionError("down"), OSError("still down"), TRADE_KEY)
    mode, _ = live_trader.determine_mode(client, stop_event=stop, retry=7, log=log.append)
    assert mode == "live" and client.asked == 3 and waits == [7, 7]
    assert all("not trading" in line for line in log)

    stop.wait = lambda seconds: True  # --stop while it waits
    assert live_trader.determine_mode(KeyClient(OSError("down")), stop_event=stop) is None


def test_a_live_trader_sends_real_orders_and_marks_everything_live(tmp_path, monkeypatch):
    broker = RecordingBroker()
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc", "capital": 1000}], profiles={"btc": _hourly(1.0)},
                 mode="live", live_broker=broker)
    h.run_to(61)
    assert broker.orders == [("BUY", "BTC-USD", pytest.approx(1000.0))]
    assert h.row("btc")["mode"] == "live" and h.ledger.recent_orders()[0]["mode"] == "live"
    snapshot = h.trader.snapshot()
    assert snapshot["mode"] == "live" and snapshot["system_live"] and "LIVE TRADING" in \
        live_trader.describe_snapshot(snapshot)


def test_a_paper_trader_never_reaches_the_exchange(tmp_path, monkeypatch):
    broker = RecordingBroker()
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc", "capital": 1000, "live": True}],
                 profiles={"btc": _hourly(1.0)}, live_broker=broker)
    h.run_to(90)
    assert broker.orders == [] and len(h.ledger.trades("btc")) == 1
    assert any('"live" setting is no longer used' in note for note in h.trader.deployments["btc"].notes)


def test_live_and_paper_records_are_kept_apart(tmp_path, monkeypatch):
    paper = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc", "capital": 1000}],
                     profiles={"btc": _hourly(1.0)})
    paper.run_to(61)
    assert paper.ledger.get_meta("ledger_mode") == "paper"
    assert paper.trader.paths.ledger == tmp_path / "paper" / "ledger" / "trading_ledger.db"

    # A live trader pointed at the paper records (a wrong --ledger, say) refuses them...
    wrong = live_trader.LiveTrader(paper.trader.config, None, paper.ledger, ENV, base_dir=tmp_path, now=paper.clock,
                                   market_data=paper.market, live_broker=RecordingBroker(), mode="live")
    with pytest.raises(live_trader.LedgerModeError, match="is the paper trading ledger"):
        wrong.start()
    assert paper.row("btc")["mode"] == "paper"

    # ...and its own records start empty, in their own area: paper money never becomes real orders.
    live_ledger = TradingLedger(tmp_path / "live" / "ledger" / "trading_ledger.db")
    live = live_trader.LiveTrader(paper.trader.config, None, live_ledger, ENV, base_dir=tmp_path, now=paper.clock,
                                  market_data=paper.market, live_broker=RecordingBroker(), mode="live")
    live.start()
    assert live_ledger.deployment("btc")["units"] == 0 and live_ledger.deployment("btc")["cash"] == 1000
    assert paper.row("btc")["units"] > 0


def test_an_unstamped_ledger_with_the_other_modes_deployments_is_refused(tmp_path):
    ledger = TradingLedger(tmp_path / "old.db")
    ledger.create_deployment("btc", "BTC-USD", "live", "x.json", _hourly(1.0), 100, T0)
    with pytest.raises(live_trader.LedgerModeError, match="holds live deployments"):
        ledger.claim_mode("paper")
    assert ledger.claim_mode("live") == "live" and ledger.get_meta("ledger_mode") == "live"


def test_a_live_sell_short_of_coins_sells_what_is_there_and_stops_for_review(tmp_path, monkeypatch):
    broker = RecordingBroker()
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc_live", "capital": 1000}],
                 profiles={"btc_live": _live_profile()}, mode="live", live_broker=broker)
    h.run_to(61)
    broker.balance = {"USD": (0.0, 0.0), "BTC": (5.0, 5.0)}  # someone moved coins out
    h.run_to(90)

    assert broker.orders[-1] == ("SELL", "BTC-USD", 5.0)
    assert h.row("btc_live")["status"] == "NEEDS_REVIEW"


def test_an_unfinished_order_is_settled_from_the_exchange_on_restart(tmp_path, monkeypatch):
    broker = RecordingBroker()
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc_live", "capital": 1000}],
                 profiles={"btc_live": _live_profile()}, mode="live", live_broker=broker)
    h.ledger.record_order(h.clock.t, client_order_id="c1", deployment_id="btc_live", product_id="BTC-USD",
                          mode="live", side="BUY", reason="ENTRY_SIGNAL", exchange_order_id="X1", status="SUBMITTED")
    broker.fills["X1"] = {"status": "FILLED", "base": 2.0, "value": 200.0, "fees": 1.0, "price": 100.0,
                          "order_id": "X1"}
    h.trader = h.make()
    h.trader.start()

    row = h.row("btc_live")
    assert row["units"] == 2.0 and row["cash"] == pytest.approx(799.0)
    assert h.ledger.order("c1")["status"] == "FILLED"


def test_an_order_with_an_unknown_outcome_stops_the_deployment_until_reviewed(tmp_path, monkeypatch):
    class Unclear(RecordingBroker):
        def buy(self, *args):
            raise OrderUnknown("timed out", order_id="X9")

    broker = Unclear()
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc_live", "capital": 1000}],
                 profiles={"btc_live": _live_profile()}, mode="live", live_broker=broker)
    h.run_to(61)
    assert h.row("btc_live")["status"] == "NEEDS_REVIEW"

    broker.fills["X9"] = {"status": "FILLED", "base": 1.0, "value": 101.0, "fees": 0.0, "price": 101.0,
                          "order_id": "X9"}
    live_trader.main(["--data-dir", str(tmp_path), "--clear-review", "btc_live"])
    h.run_to(62)  # the running trader picks up the cleared review and settles the order first
    assert h.row("btc_live")["status"] == "ACTIVE" and h.row("btc_live")["units"] == 1.0


# --- settings ---------------------------------------------------------------------------------------

def test_settings_mistakes_are_explained(tmp_path):
    path = tmp_path / "live.json"

    def check(config, message):
        path.write_text(json.dumps(config))
        with pytest.raises(LiveConfigError, match=message):
            load_live_config(path)

    check({"deployments": [{"id": "a", "profile": "x.json", "capital": 1}] * 2}, "Two deployments")
    check({"deployments": [{"id": "a", "profile": "x.json", "capital": 0}]}, "capital")
    check({"deployments": [{"id": "a b", "profile": "x.json", "capital": 1}]}, "id must be")
    check({"deployments": [{"id": "a", "capital": 1}]}, "profile")
    check({"settings": {"poll_seconds": 1}}, "poll_seconds")
    with pytest.raises(LiveConfigError, match="copy the template live_config"):
        load_live_config(tmp_path / "missing.json")


def test_the_example_settings_load():
    config = load_live_config(live_trader.ROOT / "live_config" / ".example" / "live_trading.example.json")
    assert len({d["id"] for d in config["deployments"]}) == len(config["deployments"]) >= 3
    assert all(d["live"] is None for d in config["deployments"])  # the API key decides now


def test_flash_crash_ladders_are_left_out_and_noted():
    profile = _hourly(1.0)
    profile["strategy"]["flash_crash_buy"] = {"enabled": True, "levels": [{"drop": 0.3, "allocation": 1.0}]}
    traded, notes = live_trader.trading_profile(profile, 500)
    assert "flash_crash_buy" not in traded["strategy"] and "flash-crash" in notes[0]
    assert traded["capital_allocation"]["starting_capital"] == 500
    assert "flash_crash_buy" in profile["strategy"]  # the original is untouched


def test_a_changed_profile_file_keeps_trading_the_deployed_version(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "btc", "capital": 1000}], profiles={"btc": _hourly(1.0)})
    changed = _hourly(0.5)
    (tmp_path / "btc.json").write_text(json.dumps(changed))
    h.trader = h.make()
    h.trader.start()
    deployment = h.trader.deployments["btc"]
    assert deployment.profile["capital_allocation"]["target_position_fraction"] == 1.0
    assert any("profile file has changed" in note for note in deployment.notes)


def test_floor_to_rounds_down_to_the_increment():
    assert floor_to(1.23456789, "0.0001") == 1.2345
    assert floor_to(0.00009, "0.0001") == 0.0
    assert floor_to(10.019, "0.01") == 10.01
    assert floor_to(3.7, None) == 3.7


# --- Coinbase orders --------------------------------------------------------------------------------

class FakeOrderClient:
    def __init__(self, fills):
        self.fills, self.calls = list(fills), []

    def _accept(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return {"success": True, "success_response": {"order_id": f"o{len(self.calls)}"}}

    def market_order_buy(self, **kwargs):
        return self._accept("market_order_buy", **kwargs)

    def market_order_sell(self, **kwargs):
        return self._accept("market_order_sell", **kwargs)

    def limit_order_ioc_buy(self, **kwargs):
        return self._accept("limit_order_ioc_buy", **kwargs)

    def limit_order_ioc_sell(self, **kwargs):
        return self._accept("limit_order_ioc_sell", **kwargs)

    def get_order(self, order_id):
        return SimpleNamespace(order=self.fills.pop(0))

    def cancel_orders(self, order_ids):
        self.calls.append(("cancel_orders", {"order_ids": order_ids}))


INFO = {"base_increment": "0.0001", "quote_increment": "0.01", "base_min_size": 0.0001}
QUOTE = {"bid": 100.0, "ask": 100.5, "mid": 100.25}


def test_a_market_buy_spends_the_amount_rounded_to_the_quote_step():
    client = FakeOrderClient([{"status": "FILLED", "filled_size": "9.9", "filled_value": "995.0",
                               "total_fees": "5.0", "average_filled_price": "100.5"}])
    fill = CoinbaseBroker(client, sleep=lambda s: None).buy("BTC-USD", 1000.009, QUOTE, "c1", "MARKET", 0.0025,
                                                             0.009, INFO)

    assert client.calls[0] == ("market_order_buy", {"client_order_id": "c1", "product_id": "BTC-USD",
                                                    "quote_size": "1000"})
    assert fill == {"status": "FILLED", "base": 9.9, "value": 995.0, "fees": 5.0, "price": 100.5, "order_id": "o1",
                    "exchange_status": "FILLED"}


def test_a_limit_sell_caps_slippage_and_sends_the_unfilled_rest_to_market():
    client = FakeOrderClient([
        {"status": "CANCELLED", "filled_size": "1.5", "filled_value": "149.0", "total_fees": "1.3",
         "average_filled_price": "99.4"},
        {"status": "FILLED", "filled_size": "0.5", "filled_value": "49.0", "total_fees": "0.4",
         "average_filled_price": "98.0"}])
    fill = CoinbaseBroker(client, sleep=lambda s: None).sell("BTC-USD", 2.00009, QUOTE, "c2",
                                                              "LIMIT_WITH_FRICTION_MARGIN", 0.0025, 0.009, INFO)

    name, ioc = client.calls[0]
    assert name == "limit_order_ioc_sell" and ioc["base_size"] == "2" and ioc["limit_price"] == "99.75"
    assert client.calls[1] == ("market_order_sell", {"client_order_id": "c2-m", "product_id": "BTC-USD",
                                                     "base_size": "0.5"})
    assert fill["base"] == 2.0 and fill["value"] == 198.0 and fill["fees"] == pytest.approx(1.7)
    assert fill["status"] == "FILLED"


def test_rejected_and_unclear_orders_are_told_apart():
    class Rejecting(FakeOrderClient):
        def market_order_buy(self, **kwargs):
            return {"success": False, "error_response": {"message": "INSUFFICIENT_FUND"}}

    with pytest.raises(ValueError, match="INSUFFICIENT_FUND"):
        CoinbaseBroker(Rejecting([])).buy("BTC-USD", 10, QUOTE, "c", "MARKET", 0, 0, INFO)

    class Dropping(FakeOrderClient):
        def market_order_buy(self, **kwargs):
            raise TimeoutError("read timed out")

    with pytest.raises(OrderUnknown):
        CoinbaseBroker(Dropping([])).buy("BTC-USD", 10, QUOTE, "c", "MARKET", 0, 0, INFO)


def test_an_order_still_open_at_the_timeout_is_cancelled():
    clock = Clock(0)
    client = FakeOrderClient([{"status": "OPEN"}] * 3 + [{"status": "CANCELLED", "filled_size": "0"}])

    def sleep(seconds):
        clock.t += 20

    fill = CoinbaseBroker(client, order_timeout=30, sleep=sleep, clock=clock).await_order("o1")
    assert ("cancel_orders", {"order_ids": ["o1"]}) in client.calls
    assert fill["status"] == "UNFILLED"


# --- the dashboard ----------------------------------------------------------------------------------

def test_the_dashboard_serves_the_live_snapshot(tmp_path, monkeypatch):
    from engine import QuantitativeTradingEngine
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)
    engine = QuantitativeTradingEngine(base_directory=str(tmp_path / "node"), env_file=str(tmp_path / "none.env"))
    engine.live_trader = h.trader
    engine.spin_up_lan_isolated_dashboard(0)
    try:
        port = engine.web_server.server_address[1]
        live = json.loads(urlopen(f"http://127.0.0.1:{port}/api/live", timeout=5).read())
        status = json.loads(urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5).read())
    finally:
        engine.web_server.shutdown()
    assert {row["id"] for row in live["deployments"]} == {"btc_all_in", "btc_half", "eth_flat"}
    assert status["monitored_assets"] == ["BTC-USD", "ETH-USD"]
    assert status["live_trading"]["deployments"] == 3


def test_run_stops_when_asked(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    stop = threading.Event()
    stop.set()
    h.trader.run(stop)  # starts, then returns at once
    assert h.trader.snapshot()["deployments"]


# --- the web portal and headless mode ------------------------------------------------------------------

import base64  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
from urllib.error import HTTPError  # noqa: E402
from urllib.request import Request  # noqa: E402

import engine as engine_module  # noqa: E402


def _portal(tmp_path, monkeypatch, env_lines=(), host="127.0.0.1"):
    """A trader that has traded a little, with the engine's dashboard serving it; (harness, engine, base url)."""
    h = _harness(tmp_path, monkeypatch)
    h.run_to(91)
    env_file = tmp_path / "portal.env"
    env_file.write_text("\n".join(env_lines), encoding="utf-8")
    node = engine_module.QuantitativeTradingEngine(base_directory=str(tmp_path / "node"), env_file=str(env_file))
    node.live_trader = h.trader
    urls = node.spin_up_lan_isolated_dashboard(0, host=host)
    assert urls
    return h, node, f"http://127.0.0.1:{node.web_server.server_address[1]}"


def _get(url, password=None):
    request = Request(url)
    if password is not None:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
    with urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def test_the_portal_page_and_its_data(tmp_path, monkeypatch):
    h, node, base = _portal(tmp_path, monkeypatch)
    try:
        status, headers, page = _get(base + "/portal")
        trades = json.loads(_get(base + "/api/live/trades?limit=1")[2])
        orders = json.loads(_get(base + "/api/live/orders")[2])
        events = json.loads(_get(base + "/api/live/events?limit=5")[2])
        equity = json.loads(_get(base + "/api/live/equity?deployment=btc_all_in")[2])
        with pytest.raises(HTTPError) as missing:
            _get(base + "/api/live/equity")
    finally:
        node.web_server.shutdown()

    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert b"Live Trader" in page and b"lose your entire investment" in page
    assert b"https://" not in page  # self-contained: nothing loaded from the internet
    assert len(trades) == 1 and trades[0]["deployment_id"] in ("btc_all_in", "btc_half")
    assert {o["side"] for o in orders} == {"BUY", "SELL"}
    assert len(events) == 5
    assert equity and {"time", "equity", "replay_equity"} <= set(equity[0])
    assert equity[0]["time"] > equity[-1]["time"]  # newest first
    assert missing.value.code == 400


def test_the_portal_is_reachable_on_the_machines_network_address(tmp_path, monkeypatch):
    addresses = engine_module.lan_addresses()
    if not addresses:
        pytest.skip("this machine has no network address besides loopback")
    h, node, base = _portal(tmp_path, monkeypatch, host="0.0.0.0")
    try:
        port = node.web_server.server_address[1]
        status, _, page = _get(f"http://{addresses[0]}:{port}/portal")
    finally:
        node.web_server.shutdown()
    assert status == 200 and b"Live Trader" in page


def test_the_portal_asks_for_the_password_when_one_is_set(tmp_path, monkeypatch):
    h, node, base = _portal(tmp_path, monkeypatch, env_lines=["DASHBOARD_PASSWORD=s3cret"])
    try:
        with pytest.raises(HTTPError) as anonymous:
            _get(base + "/portal")
        with pytest.raises(HTTPError) as wrong:
            _get(base + "/api/live", password="guess")
        status, _, _ = _get(base + "/api/live", password="s3cret")
    finally:
        node.web_server.shutdown()
    assert anonymous.value.code == 401 and "Basic" in anonymous.value.headers["WWW-Authenticate"]
    assert wrong.value.code == 401 and status == 200


def test_clients_outside_private_networks_are_refused(tmp_path, monkeypatch):
    allowed = engine_module.client_allowed
    assert all(allowed(a) for a in ("192.168.1.20", "10.1.2.3", "172.16.0.9", "127.0.0.1", "::1", "fe80::1",
                                     "::ffff:192.168.0.4"))
    assert not any(allowed(a) for a in ("8.8.8.8", "2001:4860:4860::8888", "not an address"))
    assert allowed("8.8.8.8", allow_public=True)

    h, node, base = _portal(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_module, "client_allowed", lambda address, allow_public=False: False)
    try:
        with pytest.raises(HTTPError) as refused:
            _get(base + "/portal")
    finally:
        node.web_server.shutdown()
    assert refused.value.code == 403


def test_dashboard_settings_come_from_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_HOST=127.0.0.1\nDASHBOARD_PASSWORD=pw\nDASHBOARD_ALLOW_PUBLIC=true\n")
    config = engine_module.load_environment_config(str(env_file))
    assert (config["dashboard_host"], config["dashboard_password"], config["dashboard_allow_public"]) == \
        ("127.0.0.1", "pw", True)
    defaults = engine_module.load_environment_config(str(tmp_path / "missing.env"))
    assert defaults["dashboard_host"] == "0.0.0.0" and defaults["dashboard_allow_public"] is False


def test_headless_logging_writes_a_utf8_file(tmp_path):
    handler = live_trader.log_to_file(tmp_path / "logs" / "live.log")
    try:
        logging.getLogger("quant_trading_bot.live").warning("stopped for review ✓ 📡")
        handler.flush()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert "stopped for review ✓ 📡" in (tmp_path / "logs" / "live.log").read_text(encoding="utf-8")


def test_stop_signals_are_caught_on_this_platform():
    stop = threading.Event()
    saved = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK")
             if hasattr(signal, name)}
    try:
        installed = live_trader.stop_on_signals(stop)
        assert {"SIGTERM", "SIGINT"} <= set(installed)
        assert ("SIGBREAK" in installed) == hasattr(signal, "SIGBREAK")  # Windows
        assert ("SIGHUP" in installed) == hasattr(signal, "SIGHUP")  # Unix
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert stop.is_set()
    finally:
        for name, previous in saved.items():
            signal.signal(getattr(signal, name), previous)


def test_a_failed_start_is_retried_until_it_works(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    attempts, waits = [], []
    real_start = h.trader.start

    def flaky_start():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("network not up yet")
        real_start()

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)
            return seconds == h.trader.settings["poll_seconds"]  # stop as soon as trading has begun

    h.trader.start = flaky_start
    h.trader.run(Stop())
    assert len(attempts) == 3 and waits[:2] == [live_trader.START_RETRY_SECONDS] * 2
    assert sum("could not start" in e["message"] for e in h.ledger.events()) == 2


def test_the_portal_shows_the_trader_starting_before_its_first_decisions(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    fresh = h.make()
    fresh.publish()
    assert fresh.snapshot()["state"] == "starting" and fresh.snapshot()["deployments"] == []
    fresh.start()
    assert fresh.snapshot()["state"] == "running" and len(fresh.snapshot()["deployments"]) == 3

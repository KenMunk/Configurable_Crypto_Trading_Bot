"""Tax on realized gains: losses offsetting gains within a year, carried forward, or not offset at all."""

from datetime import datetime, timezone

import pytest

from backtest import run_backtest
from taxes import TaxLedger
from tests.test_backtest import _candle, _profile
from tests.test_friction import _monthly_candles


def _at(year, month, day=1):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def test_a_gain_sets_aside_its_full_tax_and_a_loss_releases_what_it_saves():
    ledger = TaxLedger(0.3)
    ledger.start(_at(2021, 1, 5))

    assert ledger.realize(1000, _at(2021, 2)) == pytest.approx(300)
    assert ledger.realize(-400, _at(2021, 3)) == pytest.approx(-120)  # the tax a 400 loss saves
    assert ledger.realize(-900, _at(2021, 4)) == pytest.approx(-180)  # 270 saved, but only 180 is left to release
    assert ledger.reserved == pytest.approx(0)
    assert ledger.realize(200, _at(2021, 5)) == pytest.approx(60)  # out of caution: still its full tax

    payment, = ledger.settle(_at(2022, 1, 1))  # the year: -100 net, so nothing is due and the 60 comes back
    assert (payment["tax"], payment["refund"], payment["loss_carried_forward"]) == (0, pytest.approx(60),
                                                                                    pytest.approx(100))


def test_the_year_is_paid_at_the_start_of_the_next_and_its_net_loss_carries_forward():
    ledger = TaxLedger(0.3)
    ledger.start(_at(2021, 6))
    ledger.realize(500, _at(2021, 7))
    ledger.realize(-1500, _at(2021, 8))  # 2021: a net loss of 1,000

    assert ledger.settle(_at(2021, 12, 31)) == []  # held until the year is over
    payment, = ledger.settle(_at(2022, 1, 1))
    assert (payment["year"], payment["tax"], payment["loss_carried_forward"]) == (2021, 0, pytest.approx(1000))

    assert ledger.realize(1600, _at(2022, 3)) == pytest.approx(480)  # set aside in full during the year...
    payment, = ledger.settle(_at(2023, 2))
    assert payment["tax"] == pytest.approx(0.3 * 600)  # ...but only 600 of it was taxable after 2021's loss
    assert payment["refund"] == pytest.approx(300) and payment["loss_carried_forward"] == 0
    assert ledger.paid == pytest.approx(180)


def test_without_carryforward_a_years_loss_only_offsets_that_year():
    ledger = TaxLedger(0.3, carryforward=False)
    ledger.start(_at(2021, 6))
    ledger.realize(-1000, _at(2021, 7))
    ledger.settle(_at(2022, 1))

    assert ledger.realize(1000, _at(2022, 3)) == pytest.approx(300)


def test_years_without_trades_are_settled_one_by_one():
    ledger = TaxLedger(0.3)
    ledger.start(_at(2021, 6))
    ledger.realize(-1000, _at(2021, 7))

    payments = ledger.settle(_at(2024, 3))

    assert [p["year"] for p in payments] == [2021, 2022, 2023]
    assert all(p["loss_carried_forward"] == pytest.approx(1000) for p in payments)


def test_without_loss_offsets_each_winning_trade_is_taxed_and_paid_monthly():
    ledger = TaxLedger(0.3, loss_offset=False)
    ledger.start(_at(2021, 1, 5))

    assert ledger.realize(1000, _at(2021, 1, 10)) == pytest.approx(300)
    assert ledger.realize(-5000, _at(2021, 1, 20)) == 0  # a loss releases nothing
    payment, = ledger.settle(_at(2021, 2))
    assert (payment["year"], payment["month"], payment["tax"]) == (2021, 1, pytest.approx(300))
    assert "loss_carried_forward" not in payment


def test_rules_describe_themselves():
    assert "losses offset gains" in TaxLedger(0.3).describe() and "carried forward" in TaxLedger(0.3).describe()
    assert "held until the year ends" in TaxLedger(0.3).describe()
    assert "not carried forward" in TaxLedger(0.3, carryforward=False).describe()
    assert "losses offsetting nothing" in TaxLedger(0.3, loss_offset=False).describe()
    assert TaxLedger(0).describe() == "no tax"


def test_by_default_a_backtests_losing_trade_gets_back_the_tax_its_loss_offsets():
    profile = _profile(
        friction_model={"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3},
        strategy={"direction": "LONG_ONLY", "entry_signal": {"all": ["close > 105", "close < 150"]},
                  "exit_signal": {"any": ["close > 190", "close < 110"]}})
    profile["capital_allocation"]["target_position_fraction"] = 0.5

    result = run_backtest(_monthly_candles(), profile)

    winning, losing = result["trades"]
    tax_due = winning["pnl"] * 0.3
    assert winning["tax_withheld"] == pytest.approx(tax_due)
    assert losing["pnl"] < -winning["pnl"]  # the loss outweighs the gain: the year owes nothing
    assert losing["tax_withheld"] == pytest.approx(-tax_due) and losing["net_pnl"] == pytest.approx(losing["pnl"] + tax_due)
    assert result["series"]["tax_reserve"][2] == pytest.approx(tax_due)
    assert result["series"]["tax_reserve"][4] == pytest.approx(0)  # released back to trading cash
    assert result["taxes_paid"] == 0 and result["tax_payments"] == []  # the year isn't over: nothing paid
    assert "losses offset gains" in result["tax_rule"]


def test_a_backtest_pays_the_years_net_tax_at_the_start_of_the_next_year():
    profile = _profile(
        friction_model={"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3},
        strategy={"direction": "LONG_ONLY", "entry_signal": {"all": ["close < 150"]},
                  "exit_signal": {"any": ["close > 190"]}})
    candles = [_candle(_at(2021, 12, 30), 100), _candle(_at(2021, 12, 31), 200),
               _candle(_at(2022, 1, 1), 100), _candle(_at(2022, 1, 2), 100)]

    result = run_backtest(candles, profile)

    gain = result["trades"][0]["pnl"]
    payment, = result["tax_payments"]
    assert (payment["year"], payment["tax"]) == (2021, pytest.approx(0.3 * gain))
    assert result["series"]["tax_reserve"] == pytest.approx([0, 0.3 * gain, 0, 0])

    from simulator import format_friction
    report = "\n".join(format_friction(result))
    assert "- 2021: net realized" in report and "Tax held for the next yearly payment" in report


def test_a_backtest_holds_the_cautious_reserve_and_returns_the_excess_at_year_end():
    # A loss, then a bigger gain, in 2021: the gain sets aside its full tax; at year end only the tax on the
    # net gain is paid and the rest goes back to trading cash.
    profile = _profile(
        friction_model={"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3},
        strategy={"direction": "LONG_ONLY", "entry_signal": {"all": ["close == 100"]},
                  "exit_signal": {"any": ["close != 100"]}})
    days = [(12, 1, 100), (12, 2, 80), (12, 3, 100), (12, 4, 150), (12, 5, 120)]
    candles = [_candle(_at(2021, month, day), price) for month, day, price in days]
    candles.append(_candle(_at(2022, 1, 1), 120))

    result = run_backtest(candles, profile)

    loss, gain = result["trades"]
    assert loss["pnl"] < 0 and loss["tax_withheld"] == 0  # nothing set aside yet to release
    assert gain["tax_withheld"] == pytest.approx(0.3 * gain["pnl"])  # the full tax, despite the earlier loss
    payment, = result["tax_payments"]
    net = loss["pnl"] + gain["pnl"]
    assert payment["tax"] == pytest.approx(0.3 * net)
    assert payment["refund"] == pytest.approx(0.3 * -loss["pnl"])
    assert result["ending_equity"] == pytest.approx(10_000 + net - 0.3 * net)  # what the year's tax leaves

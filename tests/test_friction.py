from types import SimpleNamespace

import pytest

from backtest import run_backtest
from friction import BITCOIN_TX_VBYTES, FrictionModel, fetch_friction_inputs
from tests.test_backtest import _candle, _profile

LIVE_FEES = {"maker": 0.005, "taker": 0.009, "tier": "Intro"}


def _friction_profile(**friction):
    return {
        "product_id": "BTC-USD",
        "strategy": {"exit_signal": {"profit_lock_target": 0.08}},
        "friction_model": friction,
        "safeguards": {"minimum_net_profit_gate": 0.05},
    }


def test_live_taker_rate_is_charged_on_both_fills():
    model = FrictionModel(_friction_profile(exchange_commission_rate=0.005), {"exchange_fees": LIVE_FEES})

    assert model.fill_fee_rate == 0.009
    assert model.maker_rate == 0.005
    assert "Intro" in model.fee_source_detail


def test_configured_rate_is_the_fallback_when_live_fees_are_unavailable():
    model = FrictionModel(
        _friction_profile(exchange_commission_rate=0.005),
        {"exchange_fees": None, "exchange_fee_error": "timeout"},
    )

    assert model.fill_fee_rate == 0.005
    assert "timeout" in model.fee_source_detail


def test_fee_sources_config_disabled_and_buffer_multiplier():
    config = FrictionModel(_friction_profile(
        commission_rate_source="config", fallback_commission_rate=0.004, fee_buffer_multiplier=1.25,
    ), {"exchange_fees": LIVE_FEES})
    disabled = FrictionModel(_friction_profile(exchange_fee_coverage_enabled=False), {"exchange_fees": LIVE_FEES})

    assert config.fill_fee_rate == pytest.approx(0.005)
    assert disabled.fill_fee_rate == 0.0


def test_after_tax_screen_includes_liquidity_buffer_of_fees_and_network_fee():
    network = {"sats_per_vbyte": 10.0, "btc": 10.0 * BITCOIN_TX_VBYTES / 1e8}
    model = FrictionModel(
        _friction_profile(slippage_buffer=0.0025, tax_buffer_rate=0.313),
        {"exchange_fees": LIVE_FEES, "network_fee": network},
    )
    price, position = 50_000.0, 2_500.0

    fee, slip = 0.009, 0.0025
    gross = (1 - fee) ** 2 * 1.08 * (1 - slip) / (1 + slip) - 1
    network_fraction = network["btc"] * price / position
    liquidity = 0.5 * (2 * fee + network_fraction)
    expected = (gross - liquidity) * (1 - 0.313)

    assert model.network_fee_fraction(price, position) == pytest.approx(network_fraction)
    assert model.expected_net_return(price, position) == pytest.approx(expected)
    assert expected < 0.05
    assert not model.passes_screen(price, position)


def test_screen_passes_when_the_target_clears_friction_and_tax():
    profile = _friction_profile(tax_buffer_rate=0.2)
    profile["strategy"]["exit_signal"]["profit_lock_target"] = 0.2
    model = FrictionModel(profile, {"exchange_fees": LIVE_FEES})

    assert model.passes_screen(50_000.0, 2_500.0)


def test_fetch_friction_inputs_records_failures_instead_of_raising(monkeypatch):
    monkeypatch.setattr("friction.fetch_bitcoin_network_fee", lambda: (_ for _ in ()).throw(OSError("offline")))
    client = SimpleNamespace(get_transaction_summary=lambda: SimpleNamespace(
        fee_tier={"maker_fee_rate": "0.005", "taker_fee_rate": "0.009", "pricing_tier": "Intro"}
    ))

    inputs = fetch_friction_inputs(client, _friction_profile())

    assert inputs["exchange_fees"] == LIVE_FEES
    assert inputs["network_fee"] is None
    assert "offline" in inputs["network_fee_error"]


def _yearly_candles():
    """A profitable trade in the first year and a losing one in the next."""
    year = 365 * 86_400
    prices = [100] * 6 + [101, 102, 103, 104, 105, 110] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    later = [100] * 6 + [101, 102, 103] + [90, 85, 80, 75]
    candles += [_candle(year + 2 * 86_400 + i * 900, p) for i, p in enumerate(later)]
    candles.append(_candle(2 * year + 3 * 86_400, 75))
    return candles


def test_taxes_are_settled_yearly_on_net_gains_with_losses_carried_forward():
    profile = _profile(friction_model={"exchange_commission_rate": 0.0, "slippage_buffer": 0.0,
                                       "tax_buffer_rate": 0.3})
    profile["capital_allocation"]["target_position_fraction"] = 0.5
    profile["strategy"]["exit_signal"]["profit_lock_target"] = 0.05
    profile["execution"] = {"max_orders_per_day": 2}  # no re-entry after the first year's win

    result = run_backtest(_yearly_candles(), profile)

    first, second = result["tax_payments"][:2]
    assert first["net_realized_gain"] > 0
    assert first["tax"] == pytest.approx(first["net_realized_gain"] * 0.3)
    assert second["net_realized_gain"] < 0
    assert second["tax"] == 0
    assert second["loss_carryforward"] == pytest.approx(-second["net_realized_gain"])
    assert result["taxes_paid"] == pytest.approx(first["tax"])
    realized = sum(trade["pnl"] for trade in result["trades"])
    assert result["ending_equity"] == pytest.approx(10_000 + realized - first["tax"])


def test_backtest_skips_signals_that_fail_the_friction_screen():
    prices = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
    candles = [_candle(i * 900, p) for i, p in enumerate(prices)]
    profile = _profile(safeguards={"use_hard_stop": False, "minimum_net_profit_gate": 0.05})
    profile["strategy"]["exit_signal"]["profit_lock_target"] = 0.02

    result = run_backtest(candles, profile)

    assert result["trade_count"] == 0
    assert result["screened_signals"] == 1
    assert result["friction"]["expected_net_return"] < 0.05

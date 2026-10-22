"""The progressive tax model: federal and California tax on trading gains stacked on a salary.

Every test uses the committed example profile (or its own), never the private tax/tax_profile.json.
"""

import json
from datetime import datetime, timezone

import pytest

import taxes
from backtest import run_backtest
from taxes import EXAMPLE_PROFILE, ProgressiveTaxLedger, TaxCode, load_tax_profile, load_tax_rules
from tests.test_backtest import _candle, _profile


def _code(**changes):
    return TaxCode(load_tax_rules(), {**load_tax_profile(EXAMPLE_PROFILE), **changes})


def _at(year, month, day=1):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def test_the_example_profile_is_a_single_filer_on_70000_in_california():
    profile = load_tax_profile(EXAMPLE_PROFILE)
    assert (profile["filing_status"], profile["salary"], profile["jurisdictions"]) == (
        "single", 70000, ["federal", "california"])


def test_a_short_term_gain_is_taxed_at_the_salarys_marginal_ordinary_rates():
    # 2025, single, 70,000 salary: federal taxable 54,250 sits in the 22% band; California's 64,294 in the 8% band,
    # crossing into 9.3% at 72,724.
    tax = _code().trading_tax(2025, 10_000, 0)
    assert tax["federal"] == pytest.approx(2_200)
    assert tax["california"] == pytest.approx(8_430 * 0.08 + 1_570 * 0.093)
    assert tax["niit"] == 0 and tax["total"] == pytest.approx(tax["federal"] + tax["california"])


def test_a_long_term_gain_gets_the_federal_long_term_rate_but_california_taxes_it_as_ordinary_income():
    tax = _code().trading_tax(2025, 0, 10_000)
    assert tax["federal"] == pytest.approx(1_500)  # 15%: taxable income is past the 0% band's 48,350
    assert tax["california"] == pytest.approx(_code().trading_tax(2025, 10_000, 0)["california"])


def test_big_gains_climb_the_brackets_and_pay_the_net_investment_income_tax():
    small, big = _code().trading_tax(2025, 10_000, 0)["total"], _code().trading_tax(2025, 1_000_000, 0)
    assert big["niit"] == pytest.approx(0.038 * (70_000 + 1_000_000 - 200_000))  # MAGI over 200,000
    assert big["total"] / 1_000_000 > 0.45 > small / 10_000  # about 48% at a million, about 30% at ten thousand


def test_a_net_loss_deducts_up_to_3000_from_the_salary_and_carries_the_rest_forward():
    code = _code()
    tax = code.trading_tax(2025, -10_000, 0)
    assert tax["federal"] == pytest.approx(-3_000 * 0.22) and tax["california"] == pytest.approx(-3_000 * 0.08)
    assert code.carried_loss(2025, -10_000, 0) == (pytest.approx(-7_000), 0)
    assert code.carried_loss(2025, -2_000, -5_000) == (0, pytest.approx(-4_000))  # short-term losses used first


def test_short_and_long_term_results_net_against_each_other():
    assert taxes._net_capital(-4_000, 10_000) == (0, 6_000)
    assert taxes._net_capital(5_000, -8_000) == (0, -3_000)


def test_a_salary_can_change_by_year_as_a_career_progresses():
    code = _code(salary_by_year={"2025": 250_000})
    assert code.salary(2025) == 250_000 and code.salary(2024) == 70_000
    tax = code.trading_tax(2025, 10_000, 0)
    assert tax["federal"] == pytest.approx(3_200)  # taxable 234,250: now the 32% band
    assert tax["niit"] == pytest.approx(0.038 * 10_000)  # and over the 200,000 threshold


def test_years_without_rules_use_the_nearest_year():
    code = _code()
    assert code.trading_tax(2019, 10_000, 0) == code.trading_tax(2025, 10_000, 0)
    assert code._rules(2026, "california") is code.rules["2025"]["california"]  # 2026's isn't published yet


def test_the_ledger_sets_aside_at_marginal_rates_and_trues_up_at_year_end():
    code = _code()
    ledger = ProgressiveTaxLedger(code)
    ledger.start(_at(2025, 1, 2))

    first = ledger.realize(10_000, _at(2025, 2))
    second = ledger.realize(10_000, _at(2025, 3))
    assert first == pytest.approx(code.trading_tax(2025, 10_000, 0)["total"])
    assert second > first  # the second 10,000 sits in higher bands
    released = ledger.realize(-10_000, _at(2025, 4))
    assert released == pytest.approx(-(code.trading_tax(2025, 20_000, 0)["total"] - code.trading_tax(2025, 10_000, 0)["total"]))

    payment, = ledger.settle(_at(2026, 1, 1))
    assert payment["tax"] == pytest.approx(code.trading_tax(2025, 10_000, 0)["total"])
    assert payment["refund"] == pytest.approx(first + second + released - payment["tax"])
    assert payment["federal"] + payment["niit"] + payment["california"] == pytest.approx(payment["tax"])


def test_a_loss_first_then_a_gain_sets_aside_the_gains_full_tax_and_returns_the_excess():
    ledger = ProgressiveTaxLedger(_code())
    ledger.start(_at(2025, 1, 2))
    assert ledger.realize(-5_000, _at(2025, 2)) == 0  # nothing set aside yet to release
    full = ledger.realize(10_000, _at(2025, 3))
    payment, = ledger.settle(_at(2026, 1, 1))
    assert full == pytest.approx(_code().trading_tax(2025, 10_000, 0)["total"])  # out of caution, the full tax
    assert payment["tax"] == pytest.approx(_code().trading_tax(2025, 5_000, 0)["total"])
    assert payment["refund"] == pytest.approx(full - payment["tax"])


def test_a_backtest_taxes_a_position_held_over_a_year_at_long_term_rates():
    friction = {"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3,
                "tax_profile": str(EXAMPLE_PROFILE)}
    profile = _profile(friction_model=friction,
                       strategy={"direction": "LONG_ONLY", "entry_signal": {"all": ["close < 150"]},
                                 "exit_signal": {"any": ["close > 190"]}})
    short = [_candle(_at(2025, 1, 2), 100), _candle(_at(2025, 6, 1), 200), _candle(_at(2026, 1, 1), 100)]
    long_ = [_candle(_at(2024, 1, 2), 100), _candle(_at(2025, 6, 1), 200), _candle(_at(2026, 1, 1), 100)]

    held_months, held_years = run_backtest(short, profile), run_backtest(long_, profile)

    assert held_months["tax_payments"][-1]["short_term"] > 0 and held_years["tax_payments"][-1]["long_term"] > 0
    assert held_years["taxes_paid"] < held_months["taxes_paid"]  # 15% federal instead of 22-24%
    assert "progressive federal and California" in held_months["tax_rule"]


def test_a_profile_can_keep_the_flat_rate():
    friction = {"exchange_commission_rate": 0.0, "slippage_buffer": 0.0, "tax_buffer_rate": 0.3, "tax_model": "flat"}
    profile = _profile(friction_model=friction)
    result = run_backtest([_candle(_at(2025, 1, 2), 100), _candle(_at(2025, 1, 3), 101)], profile)
    assert result["tax_rule"].startswith("30.0% of each year's net realized gain")


def test_an_unknown_tax_model_is_rejected():
    from friction import FrictionModel
    with pytest.raises(ValueError, match="tax_model"):
        FrictionModel({"friction_model": {"tax_model": "guess"}})


def test_the_private_profile_is_ignored_by_git():
    import subprocess
    from pathlib import Path
    root = Path(taxes.__file__).parent
    ignored = subprocess.run(["git", "check-ignore", "-q", "tax/tax_profile.json"], cwd=root)
    assert ignored.returncode == 0
    assert json.loads((root / "tax" / "tax_profile.example.json").read_text())["salary"] == 70000

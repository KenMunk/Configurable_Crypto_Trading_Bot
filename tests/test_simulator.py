import json

import pytest

from simulator import SimulationProfile, parse_floor


def test_simulation_profile_builds_from_json(tmp_path):
    config_path = tmp_path / "sim_profile.json"
    config_path.write_text(
        json.dumps(
            {
                "ticker": "BTC",
                "strategy_horizon": "MACRO_TREND",
                "indicators": {"fast_window_periods": 30, "slow_window_periods": 180},
                "safeguards": {"hard_stop_percentage": 0.03},
            }
        ),
        encoding="utf-8",
    )

    profile = SimulationProfile.from_json(config_path)

    assert profile["ticker"] == "BTC"
    assert profile["indicators"]["fast_window_periods"] == 30
    assert profile["safeguards"]["hard_stop_percentage"] == 0.03


def test_parse_floor():
    assert parse_floor("") is None
    assert parse_floor("   ") is None
    assert parse_floor("1000") == 1000
    assert parse_floor(" $1,500.50 ") == 1500.5
    assert parse_floor("0.25") == 0.25
    with pytest.raises(ValueError, match="must be a number"):
        parse_floor("abc")
    for bad in ("0", "-5", "inf", "nan"):
        with pytest.raises(ValueError, match="above zero"):
            parse_floor(bad)

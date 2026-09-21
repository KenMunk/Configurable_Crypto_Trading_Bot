import json

from simulator import SimulationProfile


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

import csv
import json
import math

import pytest

from model_assessor import (GoalsError, assess, assess_models, check_goals, config_models, discard, load_goals,
                            rank, restore, row_metrics, set_targets, to_discard)
from performance_log import profile_fingerprint


def _row(name="m", growth=8.0, drawdown=40.0, trades=10, ending=80_000, years=4, win=50.0, vs=10.0, window=20.0):
    return {"model": name, "product": "ETH-USD", "test_start": "2020-01-01 00:00",
            "test_end": f"{2020 + years}-01-01 00:00", "growth_multiple": str(growth), "max_drawdown_pct": str(drawdown),
            "trades": str(trades), "ending_value": str(ending), "win_rate_pct": str(win),
            "vs_buy_and_hold_pct": str(vs), "worst_in_window_pct": str(window)}


GOALS = check_goals({"growth": {"min_annual_growth_pct": 50}, "ending_value": {"min_ending_value": 50_000},
                     "risk": {"max_drawdown_pct": 50, "min_trades": 5}})


def test_metrics_annualize_growth_and_score_it_against_drawdown():
    metrics = row_metrics(_row(growth=16.0, drawdown=50.0, years=4), drawdown_weight=0.5)

    assert metrics["annual_growth_pct"] == pytest.approx(100, rel=1e-3)  # 16x over 4 years is 2x a year
    assert metrics["score"] == pytest.approx((math.log(16) + 0.5 * math.log(0.5)) / metrics["years"])


def test_a_model_meeting_every_set_target_passes():
    result = assess("m", _row(growth=16.0, drawdown=40.0, trades=10, ending=160_000), GOALS)

    assert result["passes"] and result["goals_met"] == result["goals_set"] == 4
    assert result["missed"] == []


def test_each_missed_target_is_named_with_the_value_reached():
    result = assess("m", _row(growth=16.0, drawdown=70.0, trades=3, ending=160_000), GOALS)

    assert not result["passes"] and result["goals_met"] == 2
    assert result["missed"] == ["max drawdown % <= 50 (got 70)", "trades >= 5 (got 3)"]


def test_require_any_passes_on_one_target_and_no_targets_pass_everything():
    any_goal = check_goals({"require": "any", "risk": {"max_drawdown_pct": 10, "min_trades": 5}})
    assert assess("m", _row(drawdown=70.0, trades=10), any_goal)["passes"]

    none = check_goals({})
    assert set_targets(none) == [] and assess("m", _row(), none)["passes"]


def test_a_model_without_results_ranks_last():
    ranked = rank([assess("broken", None, GOALS), assess("weak", _row(growth=1.1), GOALS),
                   assess("good", _row(growth=16.0, ending=160_000), GOALS)])

    assert [a["model"] for a in ranked] == ["good", "weak", "broken"]
    assert ranked[-1]["missed"] == ["no results"]


def test_ranking_puts_passing_models_first_then_goals_met_then_score():
    goals = check_goals({"risk": {"max_drawdown_pct": 50, "min_trades": 5}})
    risky_but_rich = assess("rich", _row(growth=100.0, drawdown=80.0), goals)
    safe = assess("safe", _row(growth=2.0, drawdown=30.0), goals)
    safer_richer = assess("safer", _row(growth=4.0, drawdown=30.0), goals)

    assert [a["model"] for a in rank([risky_but_rich, safe, safer_richer])] == ["safer", "safe", "rich"]


@pytest.mark.parametrize("goals, message", [
    ({"growth": {"min_cagr": 10}}, "unknown target growth.min_cagr"),
    ({"risk": {"max_drawdown_pct": "40%"}}, "must be a number or null"),
    ({"require": "most"}, 'require must be "all" or "any"'),
    ({"ranking": {"discard_fraction": 1.0}}, "discard_fraction"),
])
def test_goal_mistakes_are_reported(goals, message):
    with pytest.raises(GoalsError, match=message):
        check_goals(goals)


def test_the_example_goals_file_is_valid():
    from model_assessor import EXAMPLE_GOALS
    goals = load_goals(EXAMPLE_GOALS)
    assert set_targets(goals) and goals["ranking"]["discard_fraction"] == 0.8


def test_a_missing_goals_file_says_how_to_start(tmp_path):
    with pytest.raises(GoalsError, match="cp search_config/.example/goals.example.json"):
        load_goals(tmp_path / "goals.json")


def _ranked(count):
    return rank([assess(f"m{i:02d}", _row(growth=1 + i), check_goals({})) for i in range(count)])


def test_the_bottom_eighty_percent_are_chosen_but_never_below_the_minimum_kept():
    ranking = {"discard_fraction": 0.8, "min_keep": 2, "protect": []}
    assert [a["model"] for a in to_discard(_ranked(20), ranking)] == [f"m{i:02d}" for i in range(15, -1, -1)]

    assert len(to_discard(_ranked(20), {**ranking, "min_keep": 10})) == 10
    assert to_discard(_ranked(3), {**ranking, "min_keep": 10}) == []


def test_protected_models_are_never_discarded():
    chosen = to_discard(_ranked(10), {"discard_fraction": 0.8, "min_keep": 0, "protect": ["m0[0-1]", "m03"]})

    assert sorted(a["model"] for a in chosen) == ["m02", "m04", "m05", "m06", "m07"]  # the bottom 8, less 3


def _profile(product="ETH-USD"):
    return {"product_id": product, "strategy": {"entry_signal": {"all": ["close > 1"]}}}


def _write(config, name, profile):
    (config / f"{name}.json").write_text(json.dumps(profile), encoding="utf-8")


def test_discarded_models_move_to_their_currencys_discard_folder_with_a_log_and_can_be_restored(tmp_path):
    eth = tmp_path / "ETH-USD"
    eth.mkdir()
    for name in ("a", "b"):
        _write(eth, name, _profile())
    _write(tmp_path, "c", _profile())  # a profile from before currency folders
    (eth / "discard").mkdir()
    _write(eth / "discard", "b", _profile())  # an earlier model of the same name
    chosen = [assess_models([("b", eth / "b.json", _profile())], GOALS, {"b": _row(trades=1)})[0],
              assess_models([("c", tmp_path / "c.json", _profile())], GOALS, {})[0],
              assess("gone", None, GOALS)]

    moved = discard(chosen, total=3, config_dir=tmp_path)

    assert moved == ["b", "c"]  # "gone" was already moved by another run
    assert [p.name for p in eth.glob("*.json")] == ["a.json"] and not list(tmp_path.glob("*.json"))
    assert sorted(p.name for p in (eth / "discard").glob("*.json")) == ["b.json", "b_2.json", "c.json"]
    with (eth / "discard" / "discard_log.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["model"], r["product"], r["file"], r["ranked"]) for r in rows] == [
        ("b", "ETH-USD", "b_2.json", "3"), ("c", "ETH-USD", "c.json", "3")]
    assert "trades >= 5" in rows[0]["missed"] and rows[1]["missed"] == "no results"
    assert not (tmp_path / ".models.lock").exists()

    restored, problems = restore(["c", "a", "nope"], config_dir=tmp_path)
    assert restored == ["c"] and len(problems) == 2
    assert (eth / "c.json").exists()  # restored into its currency's folder


def test_config_models_read_every_currency_folder_and_skip_non_profiles(tmp_path):
    (tmp_path / "BTC-USD" / "discard").mkdir(parents=True)
    (tmp_path / ".example").mkdir()
    _write(tmp_path, "eth", _profile())
    _write(tmp_path / "BTC-USD", "btc", _profile("BTC-USD"))
    _write(tmp_path / "BTC-USD" / "discard", "dropped", _profile("BTC-USD"))
    _write(tmp_path / ".example", "template", _profile("BTC-USD"))
    (tmp_path / "BTC-USD" / "best_model.json").write_text('{"model": "btc"}', encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")

    assert [name for name, _, _ in config_models("ETH-USD", config_dir=tmp_path)] == ["eth"]
    assert [name for name, _, _ in config_models(config_dir=tmp_path)] == ["eth", "btc"]  # top level, then folders


def test_a_logged_row_for_an_edited_profile_is_not_trusted():
    profile = _profile()
    row = {**_row("m"), "profile_fingerprint": profile_fingerprint(profile)}
    edited = {**profile, "ticker": "ETH"}

    fresh, stale = assess_models([("m", None, profile), ("m", None, edited)], GOALS, {"m": row})

    assert fresh["goals_met"] >= 0 and stale["missed"] == ["no results"]


# --- the recent-period gate ------------------------------------------------------------

def _result(total=0.1, held=-0.5, drawdown=0.2, trades=4, days=365):
    return {"total_return": total, "buy_and_hold_return": held, "max_drawdown": drawdown, "trade_count": trades,
            "period_start": 0, "period_end": days * 86400}


RECENT = check_goals({"risk": {"max_drawdown_pct": 50},
                      "recent": {"start": "2025-04-01", "min_return_pct": -5, "min_vs_buy_and_hold_ratio": 1.5,
                                 "max_drawdown_pct": 42, "min_trades": 3, "score_weight": 0.5}})


def test_a_recent_period_adds_its_targets_and_its_score():
    good = assess("good", _row(drawdown=40), RECENT, _result(total=0.05, held=-0.5, drawdown=0.2, trades=4))

    assert good["passes"] and good["goals_met"] == good["goals_set"] == 5
    assert good["recent"]["vs_buy_and_hold_ratio"] == pytest.approx(1.05 / 0.5)
    plain = assess("good", _row(drawdown=40), check_goals({"risk": {"max_drawdown_pct": 50}}))
    assert good["score"] == pytest.approx(plain["score"] + 0.5 * good["recent"]["score"])


def test_a_strong_history_with_a_weak_recent_record_fails():
    weak = assess("weak", _row(growth=50.0, drawdown=40), RECENT, _result(total=-0.2, held=-0.5, trades=2))

    assert not weak["passes"]
    assert weak["missed"] == ["recent return % >= -5 (got -20)", "recent trades >= 3 (got 2)"]


def test_a_model_without_recent_results_misses_every_recent_target_and_ranks_below():
    missing = assess("missing", _row(drawdown=40), RECENT, None)
    good = assess("good", _row(drawdown=40), RECENT, _result())

    assert not missing["passes"] and len(missing["missed"]) == 4 and missing["score"] == -math.inf
    assert [a["model"] for a in rank([missing, good])] == ["good", "missing"]


@pytest.mark.parametrize("recent, message", [
    ({"start": "April 2025"}, "recent.start must be a date"),
    ({"start": "2025-04-01", "min_cagr": 1}, "unknown target recent.min_cagr"),
])
def test_recent_period_mistakes_are_reported(recent, message):
    with pytest.raises(GoalsError, match=message):
        check_goals({"recent": recent})


def test_models_ending_below_buy_and_hold_are_picked_for_the_discard_folder(tmp_path):
    from model_assessor import below_buy_and_hold
    profile = _profile()
    fingerprint = profile_fingerprint(profile)
    rows = {"behind": {**_row("behind"), "vs_buy_and_hold_pct": "-12.5", "growth_multiple": "3.5",
                       "buy_and_hold_multiple": "4", "profile_fingerprint": fingerprint},
            "ahead": {**_row("ahead"), "vs_buy_and_hold_pct": "40", "profile_fingerprint": fingerprint},
            "edited": {**_row("edited"), "vs_buy_and_hold_pct": "-50", "profile_fingerprint": "old"}}
    models = [(name, tmp_path / f"{name}.json", profile) for name in ("behind", "ahead", "edited", "unlogged")]

    losers = below_buy_and_hold(models, rows)

    assert [a["model"] for a in losers] == ["behind"]  # an edited or unlogged model has nothing to judge it on
    assert losers[0]["product"] == "ETH-USD" and "-12.5% vs buy and hold" in losers[0]["missed"][0]
